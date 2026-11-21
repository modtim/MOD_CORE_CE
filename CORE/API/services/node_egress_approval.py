"""FX.91 p2c [B] — tenant-admin ONE-TIME approval of a node's declared egress.

FX.91's contract (owner Q77=A): node containers default-deny outbound and
grant egress only through the per-task proxy bridge, so the manifest half
(p2a) declares WHICH external hostnames a node needs
(`node_definitions.egress_domains`, read through
services/node_egress_domains.get_node_egress_domains) and p2b shows that
list in the palette. p2c is the human decision in between: a tenant admin
reviews the list ONCE and the platform stops re-asking — until the node's
declaration CHANGES, at which point the approval is stale and the gate
refuses again until re-approved.

    approve(db, tenant_id, node_definition_id, domains, admin_user_id)
    revoke(db, tenant_id, node_definition_id, admin_user_id)
    is_approved(db, tenant_id, node_definition_id)

Approval is stored as models.NodeEgressApproval (one live row per
(tenant, node); migrations/20261007zz_fx91_p2c_egress_approval.py) and the
row carries the EXACT canonical domain set the admin approved, so validity
is a set comparison against the node's current declaration — never a
timestamp, never a "approved once, valid forever" flag that a widened
manifest could silently ride.

Revocation is a SOFT revoke (ISO 27001: no hard delete anywhere in this
vertical): the row keeps its evidence and stamps deleted_at/deleted_by_user_id
plus revoked_at/revoked_by_user_id; a later approval REVIVES that row.

Every change — approve, re-approve, revoke — appends an AuditEvent
('NODE_EGRESS_APPROVED' / 'NODE_EGRESS_REVOKED') carrying the before/after
domain sets, so the record of what the admin saw is independent of the row.

The enforcement read is `is_approved`; the dispatch gate that turns it into
a refusal lives in routers/worker_tasks.py (FX.91.p2c gate) and is inert
while the `nodes.egress_approval_required` platform setting is off.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import text as satext

import models
from services import node_egress_domains
# FX.91 p3b writes the blocked-egress row through the existing audit path
# (audit.log_permission_audit -> PermissionAuditLog, the stream the SIEM
# shipper reads). audit.py pulls models + services.client_ip only, so this
# module-level import adds no cycle.
import audit  # noqa: E402 — the audit path, not a name clash

from services._base import ServiceError

logger = logging.getLogger(__name__)

APPROVAL_SETTING_KEY = "nodes.egress_approval_required"
APPROVE_EVENT = "NODE_EGRESS_APPROVED"
REVOKE_EVENT = "NODE_EGRESS_REVOKED"
REQUIRED_EVENT = "NODE_EGRESS_APPROVAL_REQUIRED"


class EgressApprovalError(ServiceError):
    """Service-layer failure the p2c endpoints map to an HTTP status."""

    def __init__(self, message: str, code: str = "egress_approval_error"):
        super().__init__(message, code=code)


class ForbiddenError(EgressApprovalError):
    def __init__(self, message: str):
        super().__init__(message, code="forbidden")


class NotFoundError404(EgressApprovalError):
    def __init__(self, message: str):
        super().__init__(message, code="not_found")


def _utcnow() -> datetime:
    """Explicit UTC timestamp for the stamps this module writes.

    Deliberately NOT sqlalchemy ``func.now()``: the test fixture binds a
    Session to one connection inside an outer transaction, and a server-side
    default read on a just-inserted row there is a needless round-trip. The
    audit rows keep their own ``server_default=func.now()`` (append-only,
    never re-read), so only the approval row's stamps are set here.
    """
    return datetime.now(timezone.utc)


def _live_row(db: Session, tenant_id: Optional[int],
              node_definition_id: int, *,
              include_revoked: bool = False):
    """The approval row for (tenant, node); None when there is none.

    ``tenant_id`` NULL selects the platform/system-scope approval (system
    nodes carry tenant_id NULL on node_definitions). Rows are matched on
    the tenant scope EXACTLY — a tenant's approval never satisfies the
    platform scope and vice versa — so a tenant cannot inherit another
    tenant's decision. ``include_revoked`` pulls the tombstone too, so a
    re-approve can revive it instead of orphaning a second row.
    """
    q = (
        db.query(models.NodeEgressApproval)
        .filter(models.NodeEgressApproval.node_definition_id
                == node_definition_id)
    )
    if tenant_id is None:
        q = q.filter(models.NodeEgressApproval.tenant_id.is_(None))
    else:
        q = q.filter(models.NodeEgressApproval.tenant_id == tenant_id)
    if not include_revoked:
        q = q.filter(models.NodeEgressApproval.deleted_at.is_(None))
    return q.order_by(models.NodeEgressApproval.id).first()


def _node_row(db: Session, node_ref, tenant_id: Optional[int] = None):
    """Resolve the NodeDefinition a dispatch/decision is about; None if absent.

    ``node_ref`` is an int node id OR the node's kind/name string — the task
    queue carries ``node_id`` as a String(255) (models.ApiTaskQueue) and the
    existing enqueue hydration resolves by ``node_kind``, so the gate must
    resolve the same way. Tenant rows win over system rows (a tenant that
    re-registered a system node's kind gets ITS declaration reviewed), and
    soft-deleted rows never resolve.
    """
    q = (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.deleted_at.is_(None))
    )
    if isinstance(node_ref, int) and not isinstance(node_ref, bool):
        q = q.filter(models.NodeDefinition.id == node_ref)
    else:
        # A tenant-authored node's node_kind is NULL (create_node_definition
        # never invents one), so the queue's string ref has to match the
        # NAME too — otherwise the gate could never resolve the very nodes
        # this vertical exists to review.
        _ref = str(node_ref)
        q = q.filter(
            (models.NodeDefinition.node_kind == _ref)
            | (models.NodeDefinition.name == _ref)
        )
    if tenant_id is not None:
        nd = q.filter(models.NodeDefinition.tenant_id == tenant_id).first()
        if nd is not None:
            return nd
    return q.filter(models.NodeDefinition.tenant_id.is_(None)).first()


def approved_domains(db: Session, tenant_id: Optional[int],
                     node_definition_id: int) -> List[str]:
    """The domain set the live approval row carries ([] = none)."""
    row = _live_row(db, tenant_id, node_definition_id)
    if row is None:
        return []
    try:
        return node_egress_domains.normalise_egress_domains(
            row.domains_approved)
    except node_egress_domains.EgressDomainError:
        return []


def is_approved(db: Session, tenant_id: Optional[int],
                node_definition_id: int) -> bool:
    """True when the node's CURRENT declared set equals the approved set.

    This is the whole one-time contract: the approval stays valid until the
    node's declared domain set changes, and a changed set requires
    re-approval. A node that declares nothing (default-deny) needs no
    approval — there is nothing to grant — so it reads as approved.
    """
    nd = _node_row(db, node_definition_id, tenant_id)
    if nd is None:
        return False
    declared = node_egress_domains.get_node_egress_domains(nd)
    if not declared:
        return True
    row = _live_row(db, tenant_id, node_definition_id)
    if row is None:
        return False
    try:
        approved = node_egress_domains.normalise_egress_domains(
            row.domains_approved)
    except node_egress_domains.EgressDomainError:
        return False
    return approved == declared


def approve(db: Session, tenant_id: Optional[int], node_definition_id: int,
            domains: Optional[List[str]], admin_user_id: int, *,
            revive: bool = True) -> Dict[str, Any]:
    """Record a tenant admin's approval of ``domains`` for one node.

    ``domains`` is re-validated through the p2a grammar (the admin is
    approving what the palette showed, so a set that no longer parses is
    refused rather than stored) and stored canonically (deduped + sorted),
    which is what makes the later set comparison exact.

    An existing live row is UPDATED (one approval per node; the audit trail
    keeps every decision, so re-approval is not a second row). With
    ``revive`` a soft-revoked tombstone is revived instead. Returns the
    shape the endpoint echoes: {'approved', 'domains', 'changed',
    'approval_id'}.
    """
    nd = _node_row(db, node_definition_id, tenant_id)
    if nd is None:
        raise NotFoundError404("Node definition not found")
    try:
        canonical = node_egress_domains.normalise_egress_domains(domains)
    except node_egress_domains.EgressDomainError as exc:
        raise EgressApprovalError(
            f"Cannot approve an egress declaration that does not parse "
            f"({exc.code}): {exc.message}",
            code="bad_domains",
        ) from None

    row = _live_row(db, tenant_id, node_definition_id,
                    include_revoked=revive)
    before = approved_domains(db, tenant_id, node_definition_id)
    if row is None:
        row = models.NodeEgressApproval(
            tenant_id=tenant_id,
            node_definition_id=node_definition_id,
            approved_by_user_id=admin_user_id,
            approved_at=_utcnow(),
            domains_approved=canonical,
        )
        db.add(row)
    else:
        row.domains_approved = canonical
        row.approved_by_user_id = admin_user_id
        row.approved_at = _utcnow()
        row.revoked_at = None
        row.revoked_by_user_id = None
        row.deleted_at = None
        row.deleted_by_user_id = None
        db.add(row)
    db.add(models.AuditEvent(
        tenant_id=tenant_id,
        user_id=admin_user_id,
        event_name=APPROVE_EVENT,
        details={
            "node_definition_id": node_definition_id,
            "domains_before": before,
            "domains_after": canonical,
            "approval_id": None,
            "scope": "platform" if tenant_id is None else "tenant",
        },
    ))
    db.commit()
    db.refresh(row)
    # Stamp the id into the audit row's details without rewriting the
    # append-only event: the audit row already exists, so the id rides the
    # response only (the trail's payload is the domain set, which is what
    # the admin saw).
    return {
        "approved": True,
        "domains": canonical,
        "changed": canonical != before,
        "approval_id": row.id,
    }


def revoke(db: Session, tenant_id: Optional[int],
           node_definition_id: int, admin_user_id: int) -> Dict[str, Any]:
    """SOFT revoke of a node's egress approval (never a hard delete).

    Stamps the tombstone (deleted_at/deleted_by_user_id, the mixin) plus
    revoked_at/revoked_by_user_id so the reason the gate now refuses is
    legible, and appends the 'NODE_EGRESS_REVOKED' audit row. A node with
    no live approval is a no-op (idempotent DELETE), reported as
    changed=False.
    """
    row = _live_row(db, tenant_id, node_definition_id)
    before = approved_domains(db, tenant_id, node_definition_id)
    if row is not None:
        row.deleted_at = _utcnow()
        row.deleted_by_user_id = admin_user_id
        row.revoked_at = _utcnow()
        row.revoked_by_user_id = admin_user_id
        db.add(row)
    db.add(models.AuditEvent(
        tenant_id=tenant_id,
        user_id=admin_user_id,
        event_name=REVOKE_EVENT,
        details={
            "node_definition_id": node_definition_id,
            "domains_before": before,
            "domains_after": [],
            "scope": "platform" if tenant_id is None else "tenant",
        },
    ))
    db.commit()
    return {
        "approved": False,
        "domains": [],
        "changed": row is not None,
        "approval_id": None if row is None else row.id,
    }


def gate_reason(db: Session, tenant_id: Optional[int],
                node_ref) -> Optional[str]:
    """The DISPATCH gate: a refusal reason when approval is required and
    missing, None to proceed.

    Inert while `nodes.egress_approval_required` is off (behaviour unchanged
    byte-for-byte). When on: a node declaring NOTHING external is fine
    (default-deny, nothing to grant); a node whose CURRENT declared set
    equals the approved set is fine (the one-time approval still holds);
    anything else — never approved, revoked, or a declaration that changed
    since the approval — is refused. The reason names the node and the
    domains so the failed run is actionable.

    Never raises: a lookup failure is a LOG line + refusal (fail closed),
    never an open gate. FX.91 p3b layers the SIEM hook on this same event
    name.
    """
    if not gate_setting(db, tenant_id):
        return None
    nd = _node_row(db, node_ref, tenant_id)
    if nd is None:
        return (
            f"node egress approval: node {node_ref!r} is not resolvable "
            f"for tenant {tenant_id} (refusing dispatch)"
        )
    declared = node_egress_domains.get_node_egress_domains(nd)
    if not declared:
        return None
    if is_approved(db, tenant_id, nd.id):
        return None
    reason = (
        f"node '{nd.name}' (id {nd.id}) declares external egress "
        f"{declared} that this tenant has not approved "
        f"(nodes.egress_approval_required; re-approval needed after any "
        f"change to the declared set)"
    )
    # The blocked attempt is logged here; the AUDIT row + SIEM feed for it
    # are FX.91 p3b's job (it owns the event name REQUIRED_EVENT and must not
    # commit mid-enqueue, where a partial session is open).
    logger.error(
        "FX.91 p2c egress approval required: node=%s tenant=%s declared=%s "
        "approved=%s",
        nd.id, tenant_id, declared,
        approved_domains(db, tenant_id, nd.id),
    )
    return reason


def gate_setting(db: Session, tenant_id: Optional[int] = None) -> bool:
    """The feature switch: is approval REQUIRED for this tenant scope?

    Read through the platform-settings registry (tenant row > platform row
    > default False), so behaviour is UNCHANGED while the switch is off.
    """
    from services import platform_settings
    try:
        return bool(platform_settings.get_setting(
            APPROVAL_SETTING_KEY, db=db, tenant_id=tenant_id))
    except Exception:  # noqa: BLE001 — a settings read must not wedge dispatch
        logger.warning("egress approval setting read failed (non-fatal)",
                       exc_info=True)
        return False


def approval_state(db: Session, tenant_id: Optional[int],
                   node_definition_id: int) -> Dict[str, Any]:
    """What the tenant admin needs to SEE when reviewing one node.

    The list/detail read for the endpoints: the node's CURRENT declared set,
    the set the admin approved, and whether they still agree. 'stale' is the
    one-time contract made visible — the declaration changed after the
    approval, so the gate refuses and re-approval is required. Never raises
    (an unresolvable node reads as 'missing').
    """
    nd = _node_row(db, node_definition_id, tenant_id)
    if nd is None:
        return {"state": "missing", "declared": [], "approved": [],
                "approval_id": None, "approved_at": None,
                "approved_by_user_id": None, "revoked_at": None}
    declared = node_egress_domains.get_node_egress_domains(nd)
    row = _live_row(db, tenant_id, nd.id)
    approved = approved_domains(db, tenant_id, nd.id)
    if not declared:
        state = "not_required"
    elif row is None:
        state = "required"
    elif approved != declared:
        state = "stale"
    else:
        state = "approved"
    return {
        "state": state,
        "declared": declared,
        "approved": approved,
        "approval_id": None if row is None else row.id,
        "approved_at": None if row is None else row.approved_at,
        "approved_by_user_id": None if row is None
        else row.approved_by_user_id,
        "revoked_at": None if row is None else row.revoked_at,
    }


def pending(db: Session, tenant_id: Optional[int]) -> List[Dict[str, Any]]:
    """Nodes whose declared egress this tenant has NOT validly approved.

    The tenant-admin LIST half: everything awaiting a decision, so the admin
    can approve in one pass instead of guessing ids. Includes 'stale' rows
    (a changed declaration) because those need a decision again. Default-deny
    nodes (declaring nothing) never appear — there is nothing to grant.
    Soft-deleted node definitions are skipped.
    """
    q = (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.deleted_at.is_(None))
    )
    if tenant_id is None:
        q = q.filter(models.NodeDefinition.tenant_id.is_(None))
    else:
        q = q.filter(models.NodeDefinition.tenant_id == tenant_id)
    out: List[Dict[str, Any]] = []
    for nd in q.order_by(models.NodeDefinition.id).all():
        st = approval_state(db, tenant_id, nd.id)
        if st["state"] in ("required", "stale"):
            out.append({
                "node_definition_id": nd.id,
                "name": nd.name,
                "node_kind": nd.node_kind,
                "declared": st["declared"],
                "approved": st["approved"],
                "state": st["state"],
            })
    return out


# ============================================================================
# FX.91 p3a [B] — gov/DoD profile: declared egress BLOCKED unless allowlisted
# ============================================================================
# The last clause of FX.91's contract (owner Q77=A): "gov profile blocks
# declared domains unless allowlisted". p2a declared WHAT a node wants, p2c
# gave a tenant admin a one-time approval of that exact set — and p2c's gate
# is deliberately report-only so switching the vertical on cannot change what
# runs. p3a is the half that DOES change what runs, but only under a gov/DoD
# compliance profile, and it is NOT gated by the tenant's switches:
#
#   * `nodes.egress_approval_report_only` cannot talk a gov install out of the
#     block (report-only is the tenant's opt-in to the p2c contract, not an
#     escape hatch from the profile's posture);
#   * a tenant's approval does not widen the install's allowlist either — the
#     approval is a human decision about WHICH domains the node asked for, the
#     allowlist is the install's decision about which domains are permitted at
#     all. Both must say yes.
#
# Same forcing pattern as services/release_signing.py
# (effective_signature_enforcement: a gov profile forces "enforce"
# regardless of the stored setting) and the same gov profile ids every other
# gov posture uses: cmmc / fedramp_high_il5.
#
# The matching itself is the PURE function `gov_egress_decision(declared,
# allowlist)` — no DB, no settings, no profile lookup — so the tests can
# exercise the rules directly and FX.91 p3b (the audit + SIEM event for a
# blocked attempt) reuses the exact fields it returns. Writing that event is
# explicitly NOT this part's job.
# ----------------------------------------------------------------------------
# The gov/DoD-aligned profile ids (identical to release_signing /
# security_incidents / credential_lifetime / log_streams — one posture, one
# vocabulary; deliberately NOT fedramp_moderate, which is a different
# posture tier and does not carry the egress clause).
GOV_PROFILES = ("cmmc", "fedramp_high_il5")

ALLOWLIST_SETTING_KEY = "nodes.egress_allowlist_domains"
ENFORCED_SETTING_KEY = "nodes.egress_gov_allowlist_enforced"
#: GY.C1's own key — read here at TENANT scope for Q39=A (per-tenant CMMC).
PROFILES_SETTING_KEY = "compliance.profiles"

#: The refusal status the enqueue returns for a gov block; p3b keys its
#: audit/SIEM event on this string, so it is a stable contract, not prose.
BLOCK_STATUS = "egress_gov_blocked"
BLOCK_EVENT = "NODE_EGRESS_GOV_BLOCKED"


def _tenant_gov_profile(db: Session, tenant_id: Optional[int]) -> Optional[str]:
    """Q39=A — the gov profile forced for ONE tenant, or None (owner decision
    10-09: CMMC is per-tenant, not platform-wide).

    Mirrors `services.gov_mod_built_only.tenant_gov_profile` (FX.95 p5c) with
    the same read and the same `GOV_PROFILES` vocabulary — one posture, one
    definition of "gov". It reads `compliance.profiles` through the registry
    at TENANT scope (tenant row > platform row), so a tenant with no row falls
    through to the platform value `gov_profile_active()` already handled and
    nothing double-counts. Kept local (rather than importing p5c) because
    gov_mod_built_only imports THIS module at module scope: a reverse edge,
    even a deferred one, would make the pair mutually dependent at runtime.
    """
    if tenant_id is None:
        return None
    from services import platform_settings as psr
    try:
        value = psr.get_setting(PROFILES_SETTING_KEY, db=db, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001 — read-side guard must not raise
        logger.warning(
            "FX.91 p3a: %s tenant-scope read failed (treated as no gov "
            "profile for tenant %s): %s", PROFILES_SETTING_KEY, tenant_id, exc,
        )
        return None
    if not isinstance(value, (list, tuple)):
        return None
    for profile in value:
        if profile in GOV_PROFILES:
            return profile
    return None


def gov_profile_active(db: Session,
                       tenant_id: Optional[int] = None) -> Optional[str]:
    """The active gov/DoD profile id, or None (CE-safe: no profiles module).

    Platform-level first: an install on a gov profile caps EVERY tenant, so
    that read ignores tenant scope (same shape as release_signing's
    active_gov_profile). Q39=A then consults the TENANT-scoped `compliance.profiles`
    row, so a gov tenant gets the egress/gov caps without touching the
    platform value — and only that tenant is affected. Callers that have no
    tenant in hand keep the old platform-only behaviour (tenant_id omitted).
    """
    from utils.paid_import import optional_paid
    cprof = optional_paid("services.compliance_profiles")
    if cprof is None:
        return None
    try:
        for profile in cprof.active_profiles(db):
            if profile in GOV_PROFILES:
                return profile
    except Exception:  # noqa: BLE001 — read-side guard must not raise
        logger.warning(
            "FX.91 p3a: gov profile lookup failed (treated as NO gov profile "
            "for the p2c half; the gov block itself is fail-closed below)",
        )
    return _tenant_gov_profile(db, tenant_id)


def _covers(rule: str, declared: str) -> bool:
    """Pure: does ONE allowlist entry permit ONE declared domain?

    Both sides are canonical p2a-grammar entries (plain hostname, or a
    leading ``*.`` wildcard whose suffix has 2+ labels). Coverage is
    scope containment — the allowlist entry must permit EVERY host the
    declared entry can reach, never merely one of them:

      exact  -> exact   only an identical hostname;
      exact  -> wildcard  NEVER (a declared wildcard can reach hosts the
                          exact entry does not name);
      wildcard -> exact  the hostname is a real subdomain of the wildcard's
                          suffix (the apex itself is NOT covered — same rule
                          as the Worker's egress proxy, which refuses a
                          private-IP apex under a wildcard);
      wildcard -> wildcard  the declared wildcard's scope is inside the
                          allowlist wildcard's scope (identical suffix, or
                          the declared suffix is itself a subdomain).
    """
    if rule.startswith("*."):
        suffix = rule[2:]
        if declared.startswith("*."):
            d_suffix = declared[2:]
            return d_suffix == suffix or d_suffix.endswith("." + suffix)
        return declared.endswith("." + suffix)
    if declared.startswith("*."):
        return False
    return rule == declared


def gov_egress_decision(declared: List[str],
                        allowlist: List[str]) -> Dict[str, Any]:
    """THE pure decision (no DB, no settings, no profile lookup).

    ``declared`` and ``allowlist`` are canonical p2a-grammar lists (callers
    normalise first). Returns the shape p3b's audit/SIEM event consumes:

      {'blocked': bool, 'denied': [...], 'allowed': [...],
       'declared': [...], 'allowlist': [...]}

    An empty allowlist blocks EVERY declared domain (fail closed); a node
    declaring nothing blocks nothing (default-deny — there is nothing to
    grant). Never raises.
    """
    _d = sorted(set(declared or []))
    _a = sorted(set(allowlist or []))
    allowed: List[str] = []
    denied: List[str] = []
    for dom in _d:
        if any(_covers(rule, dom) for rule in _a):
            allowed.append(dom)
        else:
            denied.append(dom)
    return {
        "blocked": bool(denied),
        "denied": denied,
        "allowed": allowed,
        "declared": _d,
        "allowlist": _a,
    }


def validate_egress_allowlist(value: Any) -> List[str]:
    """Strict validator for the allowlist setting (raises ValueError).

    Reuses the p2a grammar verbatim — plain hostnames and leading ``*.``
    wildcards only, no scheme/port/path/IP, no bare ``*``, no ``*.com`` —
    so the allowlist and the node declarations are the SAME vocabulary and
    a malformed list is refused at the settings WRITE path (400) instead of
    silently loosening the gov block. Returns the canonical (deduped,
    sorted) list for callers that want it.
    """
    try:
        return node_egress_domains.normalise_egress_domains(value)
    except node_egress_domains.EgressDomainError as exc:
        raise ValueError(
            f"{ALLOWLIST_SETTING_KEY} entry rejected ({exc.code}): "
            f"{exc.message}"
        ) from None


def allowlist_domains(db: Session,
                      tenant_id: Optional[int] = None) -> List[str]:
    """The domain allowlist in force for this scope (tenant row > platform).

    Read-side defensive by design: a hand-edited JSONB list that no longer
    parses reads as EMPTY — nothing allowlisted, so the gov block gets
    STRICTER, never looser. The strict half is the write path
    (validate_egress_allowlist, reached through the settings registry).
    """
    from services import platform_settings
    try:
        raw = platform_settings.get_setting(
            ALLOWLIST_SETTING_KEY, db=db, tenant_id=tenant_id,
        )
        return node_egress_domains.normalise_egress_domains(raw)
    except Exception:  # noqa: BLE001 — a bad list must not open the gate
        logger.warning(
            "FX.91 p3a: egress allowlist read failed/unparsable "
            "(treated as EMPTY = everything blocked)",
        )
        return []


def gov_enforced(db: Session, tenant_id: Optional[int] = None) -> bool:
    """Is the gov allowlist block in force? A gov profile FORCES it True.

    ``nodes.egress_gov_allowlist_enforced`` lets a NON-gov install record
    the posture ahead of time; while a gov/DoD profile is active the
    profile's requirement wins (same forcing as release_signing's
    signature enforcement), so no tenant/platform write can switch the
    block off on a gov install.
    """
    if gov_profile_active(db, tenant_id) is not None:
        return True
    from services import platform_settings
    try:
        return bool(platform_settings.get_setting(
            ENFORCED_SETTING_KEY, db=db, tenant_id=tenant_id,
        ))
    except Exception:  # noqa: BLE001 — a settings read must not wedge dispatch
        logger.warning("FX.91 p3a: enforcement setting read failed (non-fatal)")
        return False


def gov_gate_decision(db: Session, tenant_id: Optional[int],
                      node_ref) -> Dict[str, Any]:
    """The DB half: resolve the node, read the allowlist, run the pure rule.

    Returns gov_egress_decision's shape plus the resolution context
    (node name/id, profile, enforced) so p3b's audit row has every field
    it needs without re-reading anything. Never raises: an unresolvable
    node is reported as such (the CALLER decides — the p2c gate refuses a
    node it cannot resolve while its switch is on, and this half refuses
    outright when the gov posture is enforced).
    """
    nd = _node_row(db, node_ref, tenant_id)
    profile = gov_profile_active(db, tenant_id)
    enforced = gov_enforced(db, tenant_id)
    if nd is None:
        return {
            "blocked": False, "denied": [], "allowed": [],
            "declared": [], "allowlist": allowlist_domains(db, tenant_id),
            "node_definition_id": None, "node_name": None,
            "profile": profile, "enforced": enforced,
            "unresolvable": True,
        }
    declared = node_egress_domains.get_node_egress_domains(nd)
    decision = gov_egress_decision(
        declared, allowlist_domains(db, tenant_id),
    )
    decision["node_definition_id"] = nd.id
    decision["node_name"] = nd.name
    decision["profile"] = profile
    decision["enforced"] = enforced
    decision["unresolvable"] = False
    return decision


def gov_gate_reason(db: Session, tenant_id: Optional[int],
                    node_ref) -> Optional[str]:
    """The gov hard block: a refusal reason when a declared domain is not
    allowlisted, None otherwise. Ignores report_only and any approval.

    Inert (returns None) while the gov posture is not enforced — a
    non-gov install with the posture setting off behaves EXACTLY as p2c
    shipped, byte for byte. When enforced, every declared domain outside
    the allowlist is named in the reason so the failed run is actionable.
    """
    if not gov_enforced(db, tenant_id):
        return None
    d = gov_gate_decision(db, tenant_id, node_ref)
    if d["unresolvable"]:
        # Fail closed: the gov posture cannot vouch for a node it cannot
        # resolve, and the p2c gate's own unresolvable-node refusal only
        # fires while its switch is on.
        return (
            f"node egress gov block: node {node_ref!r} is not resolvable for "
            f"tenant {tenant_id} (refusing dispatch under compliance profile "
            f"{d['profile'] or 'gov posture'})"
        )
    if not d["blocked"]:
        return None
    logger.error(
        "FX.91 p3a egress gov block: node=%s tenant=%s profile=%s denied=%s "
        "declared=%s allowlist=%s (status %s)",
        d["node_definition_id"], tenant_id, d["profile"], d["denied"],
        d["declared"], d["allowlist"], BLOCK_STATUS,
    )
    return (
        f"node '{d['node_name']}' (id {d['node_definition_id']}) declares "
        f"external egress {d['denied']} that is NOT on the install's "
        f"nodes.egress_allowlist_domains allowlist (compliance profile "
        f"{d['profile'] or 'gov posture'}; blocked regardless of tenant "
        f"approval and of nodes.egress_approval_report_only; status "
        f"{BLOCK_STATUS})"
    )


# ============================================================================
# FX.91 p5 (owner decision 10-07, Tim Q5=A) — UNDECLARED NODES: NO EXTERNAL
# NETWORK UNDER A GOV PROFILE (fail closed)
# ============================================================================
# p3a capped what a node may DECLARE — a declared hostname outside
# nodes.egress_allowlist_domains is a hard block — but it is INERT for a node
# that declares NOTHING: with an empty `egress_domains` there is nothing to
# block, so such a node still got external reach whenever the WORKER promoted
# it to the main network via the API_KEY / MOD_TASK_TOKEN_URL / NIM_API_KEY
# markers (CORE/Worker/internal/executor/netpol.go:104/116/123 — all three
# branch BEFORE the AllowedEgress check at :138). Q5=A closes that hole:
# under an active gov/DoD profile, a node declaring NO egress_domains gets NO
# EXTERNAL network at all.
#
# "No EXTERNAL network", not "no network": the node must still report results,
# so the MOD API / task-token callback has to stay reachable. The worker
# already has an internal-only posture net for exactly this —
# `mod_stack-api-net` (Internal: true, the platform API attached, no internet;
# services/k8s_stack_netpol.py:136 mode 'api' is the same posture and the k8s
# backend already enforces it that way). So the p5 worker half re-points the
# three keyed branches at that net when the node declares nothing, instead of
# widening them to the main network.
#
# The API half of the decision is this pure function plus the settings read:
# the enqueue gate (routers/worker_tasks._node_egress_gov_gate) stamps the
# verdict onto the task payload so the worker never has to re-derive it, and
# a node that declares NOTHING under gov is refused-with-audit only when the
# install cannot honour the internal-only posture. The knob
# (nodes.egress_gov_undeclared_fail_closed, registry default True) is FORCED
# on by cmmc + fedramp_high_il5 and inert with NO gov profile active unless an
# admin wrote it explicitly, so a non-gov install is unchanged byte-for-byte.
UNDECLARED_SETTING_KEY = ""  # CE: knob not shipped
INTERNAL_ONLY_NETWORK = "mod_stack-api-net"
UNDECLARED_REASON = "undeclared_gov"


def gov_undeclared_fail_closed(db: Session,
                               tenant_id: Optional[int] = None) -> bool:
    """Is the p5 fail-closed posture in force? A gov profile FORCES it True.

    Same shape as `gov_enforced`: while a gov/DoD profile is active the
    profile's posture wins and no tenant/platform write can switch this off;
    the knob exists so a NON-gov install can adopt the posture ahead of time.
    Default True (fail closed) — a failed settings read keeps it ON, never
    opens the gate.
    """
    # Q5=A is scoped to gov/DoD profiles: with NO gov profile active the
    # posture never applies and non-gov behaviour is unchanged byte-for-byte
    # (requirement 5). Under an active gov profile the profile's posture wins —
    # the knob (default True) can never be switched off there — so the read
    # below is the "can this install honour the posture at all" half: a failed
    # settings read keeps it ON (fail closed), never opens the gate.
    if gov_profile_active(db, tenant_id) is None:
        # Requirement 5: non-gov behaviour is UNCHANGED. The registry default is
        # True (so a gov install has the posture without anyone writing it, and
        # the profile lock below refuses turning it off), but on a non-gov
        # install the posture only applies once an admin has EXPLICITLY written
        # the knob — "the knob exists so a non-gov install can adopt the
        # posture ahead of time". No row written = inert, byte-for-byte.
        return _explicit_non_gov_opt_in(db, tenant_id)
    from services import platform_settings
    try:
        return bool(platform_settings.get_setting(
            UNDECLARED_SETTING_KEY, db=db, tenant_id=tenant_id,
        ))
    except Exception:  # noqa: BLE001 — a settings read must not open the gate
        logger.warning(
            "FX.91 p5: undeclared-fail-closed setting read failed "
            "(kept ON under an active gov profile, fail closed)",
        )
        return True


def _explicit_non_gov_opt_in(db: Session,
                             tenant_id: Optional[int] = None) -> bool:
    """Non-gov opt-in: True only when an admin WROTE the knob (no row = inert).

    The registry default is True — that default exists for the gov half (a
    gov install gets the posture without anyone writing it, and the profile
    lock refuses clearing it). On a non-gov install the default must NOT
    change anything (FX.91 requirement 5), so the posture follows an explicit
    row at tenant scope (tenant row > platform row) or a platform row.
    """
    try:
        rows = db.query(models.PlatformSetting.key,
                        models.PlatformSetting.scope,
                        models.PlatformSetting.tenant_id,
                        models.PlatformSetting.value).filter(
            models.PlatformSetting.key == UNDECLARED_SETTING_KEY,
        ).all()
    except Exception:  # noqa: BLE001 — a failed read keeps it inert
        return False
    # Value-based, tenant row > platform row: an explicit write decides, and
    # NO row means inert (the registry default True is the GOV half only).
    platform_value = None
    tenant_value = None
    for _k, scope, row_tenant, raw in rows:
        if scope == "platform" and row_tenant is None:
            platform_value = raw
        elif tenant_id is not None and scope == "tenant" and row_tenant == tenant_id:
            tenant_value = raw
    chosen = tenant_value if tenant_value is not None else platform_value
    if chosen is None:
        return False
    return bool(chosen) is True

def gov_undeclared_decision(declared: List[str],
                            allowlist: List[str]) -> Dict[str, Any]:
    """THE pure p5 rule (no DB, no settings, no profile lookup).

    ``declared`` is the node's canonical p2a list. Returns
    ``{'undeclared': bool, 'external': bool, 'hosts': [...]}``:

      * a node declaring NOTHING is ``undeclared`` — under gov + fail-closed it
        gets NO external network ('external' False, no hosts);
      * a node that DOES declare keeps p3a's rule: its external reach is
        exactly the declared set, and the allowlist half is p3a's gate (this
        function does not re-decide it — it reports what the node asked for).

    Never raises.
    """
    _d = sorted(set(declared or []))
    return {
        "undeclared": not _d,
        "external": bool(_d),
        "hosts": _d,
    }


#: container_env marker the enqueue gate stamps onto a gov task whose node
#: declares NOTHING (see gov_undeclared_gate). netpol.go reads it and parks
#: the container on the internal-only posture net instead of promoting it to
#: the main network via the API_KEY / MOD_TASK_TOKEN_URL / NIM_API_KEY markers.
#: Listed in routers/worker_tasks.py's TENANT_RESERVED_CONTAINER_ENV so a
#: tenant cannot set it on its own payload — the API stamps it, never the
#: caller. It can only ever RESTRICT a container, never widen one.
INTERNAL_ONLY_MARKER = "MOD_GOV_UNDECLARED_INTERNAL_ONLY"

#: The payload keys that ask for EXTERNAL network. A node declaring nothing
#: that carries one of these is the fail-closed BLOCK (reason
#: `undeclared_gov`): the task would dial out on behalf of a node whose
#: manifest vouches for nothing.
EXTERNAL_REQUEST_ENV_KEYS = ("NEEDS_INTERNET",)

#: The payload keys that ask for API / worker-token reach. These are the
#: markers netpol.go turns into a MAIN-network promotion today (netpol.go:104
#: API_KEY, :116 MOD_TASK_TOKEN_URL, :123 NIM_API_KEY — plus MOD_NEEDS_API_TOKEN,
#: which cmd/worker/main.go:1383 turns into MOD_TASK_TOKEN_URL). p5 re-points
#: that promotion at the internal-only posture net when the node declares
#: nothing: the callback stays, the internet goes.
KEYED_CALLBACK_ENV_KEYS = (
    "API_KEY", "MOD_TASK_TOKEN_URL", "MOD_NEEDS_API_TOKEN", "NIM_API_KEY",
)


def _payload_external_hosts(payload: Any) -> List[str]:
    """Hostnames the TASK asks for, from the legacy dispatch shapes.

    Reads the payload's `allowed_egress` in BOTH shapes the queue has
    historically carried: the p2a flat list of host strings and the #87
    {host,ports,protocol} dicts (schemas.py:1826 / worker_tasks.py
    _compile_tailnet_egress_cfg's input). Never raises — an unparseable
    shape yields [] (nothing claimed), which is still `undeclared` for the
    env-marker half of the check.
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("allowed_egress")
    out: List[str] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str):
                out.append(entry)
            elif isinstance(entry, dict):
                host = entry.get("host")
                if isinstance(host, str):
                    out.append(host)
    return out


def gov_undeclared_gate(db: Session, tenant_id: Optional[int],
                        node_ref, payload: Any) -> Dict[str, Any]:
    """The p5 DB half: what an UNDECLARED node may reach under gov.

    Returns ``{'reason': Optional[str], 'stamp': bool, 'decision': {...}}``
    and never raises:

      * inert (reason None, stamp False, decision None) while the gov posture
        is not enforced (no gov profile, or the knob off on a non-gov install)
        — behaviour byte-for-byte unchanged;
      * a node that DECLARES domains is p3a's case, not p5's (reason None,
        stamp False) — the worker keeps its existing verdict;
      * an UNDECLARED node under gov + fail-closed:
          - if the task asks for external network anyway (NEEDS_INTERNET or a
            non-empty payload `allowed_egress`) → a refusal reason naming
            `undeclared_gov` (the caller emits the p3b audit/SIEM event);
          - otherwise → stamp True: the worker must give the node the
            internal-only posture net (MOD API + task-token callback
            reachable, zero internet) rather than the main network.
    """
    if not gov_undeclared_fail_closed(db, tenant_id):
        return {"reason": None, "stamp": False, "decision": None}
    nd = _node_row(db, node_ref, tenant_id)
    profile = gov_profile_active(db, tenant_id)
    if nd is None:
        # p3a's gate already refuses an unresolvable node outright when the
        # gov posture is enforced, so this half never sees one in practice;
        # if it does, refuse (fail closed) rather than stamp.
        return {
            "reason": (
                f"node egress gov block (undeclared_gov): node {node_ref!r} "
                f"is not resolvable for tenant {tenant_id}, so the install "
                f"cannot prove it declares no network access (compliance "
                f"profile {profile or 'gov posture'}; status "
                f"{BLOCK_STATUS})"
            ),
            "stamp": False,
            "decision": {
                "node_definition_id": None, "node_name": None,
                "profile": profile, "enforced": True, "unresolvable": True,
                "declared": [], "allowlist": allowlist_domains(db, tenant_id),
                "denied": [], "reason": UNDECLARED_REASON,
            },
        }
    declared = node_egress_domains.get_node_egress_domains(nd)
    if declared:
        return {"reason": None, "stamp": False, "decision": None}
    requested = _payload_external_hosts(payload)
    env = {}
    if isinstance(payload, dict):
        env = payload.get("container_env") or {}
    asks_external = bool(requested) or any(
        k in env for k in EXTERNAL_REQUEST_ENV_KEYS)
    decision = {
        "node_definition_id": nd.id, "node_name": nd.name,
        "profile": profile, "enforced": True, "unresolvable": False,
        "declared": [], "allowlist": allowlist_domains(db, tenant_id),
        "denied": sorted(set(requested)),
        "reason": UNDECLARED_REASON,
    }
    if asks_external:
        logger.error(
            "FX.91 p5 egress gov block (undeclared): node=%s tenant=%s "
            "profile=%s requested=%s declared=[] allowlist=%s (status %s) "
            "— the node's manifest vouches for NO network access",
            nd.id, tenant_id, profile, decision["denied"],
            decision["allowlist"], BLOCK_STATUS,
        )
        _asked = decision["denied"] or ["(via the NEEDS_INTERNET marker)"]
        return {
            "reason": (
                f"node '{nd.name}' (id {nd.id}) declares NO network access "
                f"(node_definitions.egress_domains is empty) but this task "
                f"asks for external hosts {_asked} — refused under the active "
                f"compliance profile {profile or 'gov posture'} "
                f"(undeclared_gov; nodes.egress_gov_undeclared_fail_closed). "
                f"Ask the node's author to declare the hosts it dials, or a "
                f"tenant admin to allowlist them on "
                f"nodes.egress_allowlist_domains; status {BLOCK_STATUS})"
            ),
            "stamp": False,
            "decision": decision,
        }
    # The keyed-callback case is p5's actual hole: the node declares NOTHING
    # and the task carries an API_KEY / MOD_TASK_TOKEN_URL / MOD_NEEDS_API_TOKEN
    # / NIM_API_KEY marker, which netpol.go turns into a MAIN-network promotion
    # (internet + platform services). Stamp the payload so the worker parks it
    # on the internal-only posture net instead: callback stays, internet goes.
    if any(k in env for k in KEYED_CALLBACK_ENV_KEYS):
        return {"reason": None, "stamp": True, "decision": decision}
    # Nothing declared, nothing asked for, no callback marker: the node already
    # lands on the default-deny user net (zero outbound, zero platform services)
    # — stamping it would ADD API reach it never requested, so leave it alone.
    return {"reason": None, "stamp": False, "decision": None}


# ============================================================================
# FX.91 p3b (owner decision Q77=A) — BLOCKED egress attempts: AUDIT + SIEM
# ============================================================================
# Both gates that can refuse a dispatch — the p2c tenant-admin approval gate
# (hard refusal, or the REPORT-ONLY finding that WOULD block) and the p3a gov
# allowlist block — record the attempt here: one append-only
# `PermissionAuditLog` row (models.AuditAction.NODE_EGRESS_BLOCKED) plus an
# immediate ship through the existing SIEM forwarder
# (services/siem_export._dispatch, the same nine-key normalized schema the
# checkpoint tick uses; the row also rides that tick's checkpoint stream).
#
# WHY a new enum member: no existing AuditAction fits. `tenant_ip_blocked` is
# an IP-allowlist refusal, `malware_upload_blocked` an upload scan,
# `security_config_error` a config fault — none names a NODE EGRESS decision,
# and the SIEM's `action` key is the enum value, so a borrowed name would
# mislabel the event in the client's stream. The member therefore gets its
# ALTER TYPE migration (migrations/20261007zzz_fx91_p3b_egress_block_audit.py)
# — `Enum(AuditAction)` stores NAMES in the native PG enum `auditaction`, so
# the live type must carry the label before any row writes it.
#
# DEDUPE (the run-retry flood): one event per (workflow run, node, gate
# decision) inside `security.egress_block_audit_window_seconds` (default 300),
# read back off the audit table itself — the same dedupe-key shape as
# audit.has_recent_leak_event and services/release_approvals._alert_exists, so
# no new table and no Redis dependency. The identity lives in the indexed
# columns (action + tenant_id + resource_type + resource_id), never in a JSONB
# query, so the check is a cheap indexed EXISTS and no JSONB operator is used.
#
# BEST EFFORT: nothing here may change the gate's decision. Every step is
# wrapped; a failed audit write or SIEM ship is a WARNING line, and the
# caller's refusal reason is returned untouched.
BLOCKED_ACTION = models.AuditAction.NODE_EGRESS_BLOCKED
WINDOW_SETTING_KEY = "security.egress_block_audit_window_seconds"
DEFAULT_WINDOW_S = 300
GOV_GATE_LABEL = "node_egress_gov"
APPROVAL_GATE_LABEL = "node_egress_approval"


def _block_window_seconds(db, tenant_id: Optional[int] = None) -> int:
    """The dedupe window from the platform-settings registry (never raises)."""
    from services import platform_settings
    try:
        v = int(platform_settings.get_setting(
            WINDOW_SETTING_KEY, db=db, tenant_id=tenant_id))
    except Exception:  # noqa: BLE001 — a settings read must not wedge the emit
        return DEFAULT_WINDOW_S
    return v if v > 0 else DEFAULT_WINDOW_S


def _block_actor_user_id(db, tenant_id: Optional[int],
                        workflow_run_id: Optional[int]) -> int:
    """BUG.B80 — the actor a blocked-egress audit row is attributed to.

    ``PermissionAuditLog.actor_user_id`` is a NOT-NULL FK to ``users.id``
    (models.py), so a made-up actor (the old literal ``0``) FK-violates on
    any FK-enforcing DB: the INSERT fails, and because the audit helper
    commits inside the CALLER's session, the failed flush left the
    orchestrator's session aborted — every run whose node hit a blocked
    egress became a zombie (run stuck ``running``, all NodeRuns QUEUED,
    "Workflow orchestration failed for run N").

    The real actor when the caller knows the run is the run's triggering
    user (``WorkflowRun.triggered_by_user_id`` — the same authority the
    notification/audit paths in routers/worker_tasks.py use). With no run
    to point at (platform-scope emit, a bare enqueue with no run id) the
    established system-actor convention stays ``0`` — audit.py's own
    ``actor_id = user_id or 0`` — and the SAVEPOINT around the write now
    makes even that FK miss harmless to the caller.
    """
    if workflow_run_id is None:
        return 0
    run = (
        db.query(models.WorkflowRun)
        .filter(models.WorkflowRun.id == workflow_run_id)
        .first()
    )
    actor = getattr(run, "triggered_by_user_id", None) if run else None
    return int(actor) if actor else 0


def _recent_block_event(db, tenant_id: Optional[int],
                        node_definition_id: Optional[int],
                        gate: str, window_s: int) -> bool:
    """True when this (tenant, gate, node) already fired inside the window.

    The dedupe key, read off the audit table (append-only, so a previous
    event is never rewritten or deleted — ISO 27001). An unresolvable node
    (id None) dedupes on the gate alone, so a storm of unresolvable refs
    still emits one row per window.
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_s)
    q = db.query(models.PermissionAuditLog).filter(
        models.PermissionAuditLog.action == BLOCKED_ACTION,
        models.PermissionAuditLog.resource_type == gate,
        models.PermissionAuditLog.timestamp >= cutoff,
    )
    if tenant_id is None:
        q = q.filter(models.PermissionAuditLog.tenant_id == 0)
    else:
        q = q.filter(models.PermissionAuditLog.tenant_id == tenant_id)
    q = q.filter(models.PermissionAuditLog.resource_id ==
                 (node_definition_id or 0))
    return db.query(q.exists()).scalar() is True


def record_blocked_egress(db, *, tenant_id: Optional[int],
                          decision: Dict[str, Any], gate: str,
                          report_only: bool = False,
                          workflow_run_id: Optional[int] = None,
                          node_run_id: Optional[int] = None,
                          task_id: Optional[int] = None,
                          request: Optional[Any] = None) -> Dict[str, Any]:
    """AUDIT + SIEM for one blocked (or report-only WOULD-block) attempt.

    Returns {'emitted', 'deduped', 'audit_id', 'siem'} — never raises, and
    never changes what the CALLER decided: the gate's refusal reason is the
    caller's return value, this is a side effect beside it.

    The row: models.PermissionAuditLog with action NODE_EGRESS_BLOCKED,
    resource_type = the gate that fired (so the dedupe key and the SIEM's
    `resource` both name the decision), resource_id = the node definition id
    (0 when unresolvable). BUG.B80: actor_user_id is a NOT-NULL FK to
    users.id, so the old literal 0 FK-violated and — because audit.log_
    permission_audit commits inside the CALLER's session — the failed
    INSERT aborted the orchestrator's session, zombieing every run whose
    node hit a blocked egress (run=running, all NodeRuns QUEUED). The
    actor is now the run's triggering user (WorkflowRun.triggered_by_
    user_id) when the caller passed a resolvable workflow_run_id, else
    the established system actor (0, audit.py's own `user_id or 0`), and
    the write runs on a SAVEPOINT so even that fallback FK miss rolls
    back only the audit row. Metadata carries tenant, node id/name,
    profile, enforced, the denied/declared/allowlist sets, the report-
    only posture and the run/node-run/task ids when the caller has
    them. Hostnames and ids only — never headers, tokens, payload
    bodies or secrets.
    """
    out: Dict[str, Any] = {
        "emitted": False, "deduped": False, "audit_id": None, "siem": None,
        "denial_rows": 0,
    }
    try:
        node_id = decision.get("node_definition_id")
        window = _block_window_seconds(db, tenant_id)
        # The dedupe read is best effort too: a failed read means "no recent
        # event" (emit anyway) — a lost duplicate is a louder trail, a lost
        # event is a compliance gap.
        try:
            recent = _recent_block_event(db, tenant_id, node_id, gate, window)
        except Exception as exc:  # noqa: BLE001 — fail loud, emit anyway
            logger.warning(
                "FX.91 p3b egress-block dedupe read failed (emitting): %s",
                exc,
            )
            recent = False
        if recent:
            out["deduped"] = True
            logger.info(
                "FX.91 p3b egress-block audit DEDUPED (gate=%s node=%s "
                "tenant=%s within %ss)",
                gate, node_id, tenant_id, window,
            )
            return out

        metadata: Dict[str, Any] = {
            "gate": gate,
            "report_only": bool(report_only),
            "enforced": bool(decision.get("enforced")),
            "profile": decision.get("profile"),
            "node_definition_id": node_id,
            "node_name": decision.get("node_name"),
            "unresolvable": bool(decision.get("unresolvable")),
            "denied": list(decision.get("denied") or []),
            "declared": list(decision.get("declared") or []),
            "allowlist": list(decision.get("allowlist") or []),
            # FX.91 p5: WHICH gov rule fired — p3a's declared/allowlist block
            # carries no reason key (unchanged), p5's undeclared block names
            # `undeclared_gov`, so a SIEM consumer can tell the two halves of
            # the same gov posture apart without re-deriving anything.
            "reason": decision.get("reason"),
            "workflow_run_id": workflow_run_id,
            "node_run_id": node_run_id,
            "task_id": task_id,
            "scope": "platform" if tenant_id is None else "tenant",
        }
        # BUG.B80 — the audit write runs on its OWN savepoint. The live
        # zombie: the row was written with actor_user_id=0, which FK-violates
        # against users.id (models.PermissionAuditLog.actor_user_id is a NOT
        # NULL FK); audit.log_permission_audit commits inside the CALLER's
        # session, so the failed INSERT aborted that session and the
        # orchestrator's next DB access raised PendingRollbackError — the
        # whole run failed (status=running, every NodeRun QUEUED forever).
        # A savepoint means any audit failure — FK miss, IntegrityError, dead
        # connection — rolls back ONLY the audit row and the caller's session
        # stays usable, so the blocked outcome is still returned and the run
        # finalises. Explicit savepoint object, not `with db.begin_nested():`,
        # because the audit helper commits internally (committing the
        # savepoint itself) and a context-managed savepoint rejects anything
        # after that inner commit — same shape as asset_inventory._write_alert.
        savepoint = db.begin_nested()
        try:
            audit_row = audit.log_permission_audit(
                db=db,
                action=BLOCKED_ACTION,
                actor_user_id=_block_actor_user_id(db, tenant_id,
                                                   workflow_run_id),
                tenant_id=tenant_id if tenant_id is not None else 0,
                resource_type=gate,
                resource_id=int(node_id or 0),
                metadata=metadata,
                request=request,
            )
            if audit_row is None:
                raise RuntimeError("blocked-egress audit row not written")
            out["emitted"] = True
            out["audit_id"] = audit_row.id
        except Exception as exc:  # noqa: BLE001 — the SAVEPOINT rollback path
            try:
                savepoint.rollback()
            except Exception:  # already released by the audit write itself
                pass
            logger.warning(
                "FX.91 p3b blocked-egress audit write failed (decision "
                "unchanged, caller session intact): %s", exc,
            )
            return out
        # Fortress 1.3 slice b — the dedicated per-host sink, only for a REAL
        # refusal (a report-only WOULD-block is not a denial). Best effort:
        # its own savepoint, so a missing table (the migration is not applied
        # live yet) or an FK miss rolls back only these rows.
        if not report_only:
            out["denial_rows"] = _write_egress_denial_rows(
                db, tenant_id=tenant_id, decision=decision, gate=gate,
                report_only=report_only, workflow_run_id=workflow_run_id,
                node_run_id=node_run_id,
            )
        out["siem"] = _ship_blocked_egress(db, audit_row)
        return out
    except Exception as exc:  # noqa: BLE001 — audit/SIEM must not move the gate
        logger.warning(
            "FX.91 p3b blocked-egress audit/SIEM emit failed (decision "
            "unchanged): %s", exc,
        )
        return out


# ============================================================================
# Fortress 1.3 (slice b) — the dedicated `egress_denial_log` sink
# ============================================================================
# `egress_denial_log` (migrations/20261012q_fortress_13_access_denial_logs.py)
# is append-only and has NO models.py ORM class (models.py is lead-only), so
# this writer is raw SQL. One row per REFUSED destination — the audit row
# above carries the whole denied set as JSONB metadata; this table is the
# per-host sink a compliance client queries by (tenant, created_at) or by
# run id. The migration is NOT applied live yet, so the write MUST tolerate
# UndefinedTable without poisoning the caller's session: it runs on its own
# SAVEPOINT, and any failure (missing table, FK miss, dead connection) is a
# WARNING naming only the exception CLASS (hostnames already went to the
# audit row; the exception text could carry a DB query string).
DENIAL_TABLE = "egress_denial_log"
DENIAL_ROW_CAP = 50


def _write_egress_denial_rows(db, *, tenant_id: Optional[int],
                              decision: Dict[str, Any], gate: str,
                              report_only: bool = False,
                              workflow_run_id: Optional[int] = None,
                              node_run_id: Optional[int] = None) -> int:
    """One append-only row per refused host; returns the row count (never raises).

    Skipped (0) for a platform-scope emit (tenant_id None — the table's
    tenant_id is NOT NULL) and for a report-only WOULD-block (not a denial).
    Hosts are normalized (strip/lower, optional ":port" → destination_port,
    empties dropped) and capped at 50 per call so a flood cannot grow the
    table unbounded. `node_id` is the node DEFINITION id — the same value
    the audit row stores as resource_id (`decision["node_definition_id"]`),
    stringified for the TEXT column, NULL when unresolvable. `rule` is the
    gate that fired.

    COMMIT ORDERING: never commits. The audit helper above already committed
    its own savepoint, so these rows ride the CALLER's next commit (or the
    fixture's); the savepoint means a failure here rolls back only these
    rows and leaves the caller's session usable.
    """
    if tenant_id is None or report_only:
        return 0
    rows: List[Tuple[str, Optional[int]]] = []
    for raw in (decision.get("denied") or []):
        host = str(raw or "").strip().lower()
        # Never persist credentials: a host string carried as `user:pass@host`
        # (or `scheme://user:pass@host`) must not land in this table — keep
        # only what follows the LAST '@' (userinfo cannot itself contain '@').
        if "@" in host:
            host = host.rpartition("@")[2]
        port: Optional[int] = None
        if ":" in host:
            base, _, maybe = host.rpartition(":")
            maybe = maybe.strip()
            if maybe.isdigit():
                host, port = base.strip(), int(maybe)
        host = host.strip()
        if not any(c.isalnum() for c in host):
            continue
        rows.append((host, port))
        if len(rows) >= DENIAL_ROW_CAP:
            break
    if not rows:
        return 0
    node_id = decision.get("node_definition_id")
    node_id = str(node_id) if node_id not in (None, "") else None
    written = 0
    try:
        with db.begin_nested():
            for host, port in rows:
                db.execute(satext(
                    f"""
INSERT INTO {DENIAL_TABLE} (
    tenant_id, workflow_run_id, node_run_id, node_id,
    destination_host, destination_port, rule
)
VALUES (
    :tenant_id, :workflow_run_id, :node_run_id, :node_id,
    :destination_host, :destination_port, :rule
)
"""
                ), {
                    "tenant_id": tenant_id,
                    "workflow_run_id": workflow_run_id,
                    "node_run_id": node_run_id,
                    "node_id": node_id,
                    "destination_host": host,
                    "destination_port": port,
                    "rule": gate,
                })
                written += 1
    except Exception as exc:  # noqa: BLE001 — best effort, like the audit row
        logger.warning(
            "Fortress 1.3 egress-denial row write failed (decision "
            "unchanged, caller session intact): %s", type(exc).__name__,
        )
        return 0
    return written


def _ship_blocked_egress(db, audit_row) -> Optional[Dict[str, Any]]:
    """Route the SAME event to the existing SIEM forwarder, immediately.

    The checkpoint tick (siem_export.run_export_tick) ships this row
    eventually anyway; the p3b contract is that a blocked attempt reaches
    the client's SIEM without waiting for the tick, so it rides the same
    forwarder the GY.C15b high-severity push uses: normalize_audit_row ->
    siem_export._dispatch, same nine-key schema, same Vault token, same
    egress posture. No-op (and never an error) when no SIEM is configured.
    """
    if audit_row is None:
        return None
    try:
        from utils.paid_import import optional_paid

        siem_export = optional_paid("services.siem_export")
        if siem_export is None:
            return {"shipped": False, "reason": "siem not configured"}
        cfg = {k.split(".", 1)[1]: siem_export._get_setting(k, db=db)
               for k in siem_export._SETTING_KEYS}
        if not (cfg.get("enabled") and cfg.get("endpoint")):
            return {"shipped": False, "reason": "siem not configured"}
        token = siem_export._read_siem_token_from_vault()
        event = siem_export.normalize_audit_row(audit_row)
        sent = siem_export._dispatch(cfg, [event], token)
        return {"shipped": sent > 0, "action": event["action"],
                "outcome": event["outcome"], "tenant_id": event["tenant_id"],
                "resource": event["resource"], "id": event["id"]}
    except Exception as exc:  # noqa: BLE001 — a SIEM outage must not move the gate
        logger.warning(
            "FX.91 p3b SIEM ship of blocked-egress event failed (audit row "
            "is safe, the tick retries it): %s", exc,
        )
        return {"shipped": False, "reason": str(exc)}


def approval_gate_fields(db, tenant_id: Optional[int],
                         node_ref) -> Dict[str, Any]:
    """The p2c gate's audit payload (the shape record_blocked_egress wants).

    The approval gate's decision in the same vocabulary the gov gate's
    `gov_gate_decision` returns, so ONE emit helper serves both: 'denied' is
    the declared set the tenant has NOT validly approved (what the gate
    refused), 'allowlist' is what the admin DID approve (empty when never
    approved, partial when the declaration changed since — 'stale').
    'profile'/'enforced' carry the gov posture for context because the p2c
    gate runs while the gov posture may be off. Never raises: an
    unresolvable node reports itself as such.
    """
    nd = _node_row(db, node_ref, tenant_id)
    if nd is None:
        return {
            "blocked": True, "denied": [], "allowed": [],
            "declared": [], "allowlist": [],
            "node_definition_id": None, "node_name": None,
            "profile": gov_profile_active(db, tenant_id),
            "enforced": gov_enforced(db, tenant_id),
            "unresolvable": True,
        }
    declared = node_egress_domains.get_node_egress_domains(nd)
    approved = approved_domains(db, tenant_id, nd.id)
    approved_set = set(approved)
    return {
        "blocked": not is_approved(db, tenant_id, nd.id),
        "denied": [d for d in declared if d not in approved_set],
        "allowed": [d for d in declared if d in approved_set],
        "declared": declared,
        "allowlist": approved,
        "node_definition_id": nd.id,
        "node_name": nd.name,
        "profile": gov_profile_active(db, tenant_id),
        "enforced": gov_enforced(db, tenant_id),
        "unresolvable": False,
    }


# ============================================================================
# FX.91 p2b [U] — palette read: declared egress + per-tenant approval state
# ============================================================================
# The palette (GET /v1/nodes) shows each node's declared domains (p2a's
# `egress_domains`) NEXT TO the tenant's decision on them (p2c), so the
# WorkflowBuilder can badge "Network: N domains" amber/green/red instead of
# making the admin walk GET /nodes/egress-approval before touching a node.
# ONE batched query for the whole page — the approval row is matched on
# (tenant scope, node id) exactly like `_live_row`, so a tenant never
# inherits another tenant's decision and a system node (tenant_id NULL)
# reads the platform-scope row. The declaration itself is the row's own
# column, so no per-node fetch is needed: no N+1.
def palette_egress_states(db: Session, tenant_id: Optional[int],
                          node_defs: List[Any]) -> Dict[int, Dict[str, Any]]:
    """Per-node approval state for a palette page, in ONE query.

    Returns {node_id: {'state','declared','approved','approval_id','approved_at',
    'approved_by_user_id','revoked_at','gov_blocked','gov_denied'}} — the
    same state vocabulary `approval_state` returns ('approved' / 'required'
    / 'stale' / 'not_required'), plus the p3a gov posture so the palette can
    show a red "Blocked by government profile" rather than a green approval
    the dispatch gate will refuse anyway.

    `gov_blocked` is only ever True while the gov allowlist block is ENFORCED
    (a gov profile, or `nodes.egress_gov_allowlist_enforced`), so a normal
    install pays one settings read and no allowlist read at all. Never raises:
    a settings/allowlist read failure reads as NOT blocked (the gate itself
    is the fail-closed half; this is a display hint).
    """
    ids = [nd.id for nd in node_defs if getattr(nd, "id", None) is not None]
    if not ids:
        return {}
    # Approval scope follows the NODE's scope, exactly like `_live_row`: a
    # tenant node (tenant_id = the requesting tenant) needs THAT tenant's
    # decision; a system node (tenant_id NULL) carries an install-wide
    # approval. Both halves ride ONE query, so a page of nodes is two reads
    # total (this + the settings/allowlist read), never one per node.
    q = (
        db.query(models.NodeEgressApproval)
        .filter(models.NodeEgressApproval.node_definition_id.in_(ids))
        .filter(models.NodeEgressApproval.deleted_at.is_(None))
    )
    if tenant_id is not None:
        q = q.filter(
            (models.NodeEgressApproval.tenant_id == tenant_id)
            | (models.NodeEgressApproval.tenant_id.is_(None))
        )
    else:
        q = q.filter(models.NodeEgressApproval.tenant_id.is_(None))
    # Bucket by (row scope, node id) so each node can only ever read a row
    # stamped with ITS OWN scope — a tenant's approval never satisfies a
    # system node and another tenant's decision never satisfies this one
    # (the same isolation `_live_row` gives the gate).
    rows: Dict[Tuple[Optional[int], int], Any] = {}
    for row in q.all():
        key = (row.tenant_id, row.node_definition_id)
        prev = rows.get(key)
        # One live row per (tenant, node) is enforced by the unique index;
        # the lowest id wins deterministically if a pre-migration DB ever
        # carried two.
        if prev is None or row.id < prev.id:
            rows[key] = row

    enforced = gov_enforced(db, tenant_id)
    allowlist = allowlist_domains(db, tenant_id) if enforced else []

    out: Dict[int, Dict[str, Any]] = {}
    for nd in node_defs:
        nid = getattr(nd, "id", None)
        if nid is None:
            continue
        declared = node_egress_domains.get_node_egress_domains(nd)
        row = rows.get((getattr(nd, "tenant_id", None), nid))
        approved: List[str] = []
        if row is not None:
            try:
                approved = node_egress_domains.normalise_egress_domains(
                    row.domains_approved)
            except node_egress_domains.EgressDomainError:
                approved = []
        if not declared:
            state = "not_required"
        elif row is None:
            state = "required"
        elif approved != declared:
            state = "stale"
        else:
            state = "approved"
        gov_denied: List[str] = []
        if enforced and declared:
            gov_denied = gov_egress_decision(declared, allowlist)["denied"]
        out[nid] = {
            "state": state,
            "declared": declared,
            "approved": approved,
            "approval_id": None if row is None else row.id,
            "approved_at": None if row is None else row.approved_at,
            "approved_by_user_id": None if row is None
            else row.approved_by_user_id,
            "revoked_at": None if row is None else row.revoked_at,
            "gov_blocked": bool(gov_denied),
            "gov_denied": gov_denied,
        }
    return out

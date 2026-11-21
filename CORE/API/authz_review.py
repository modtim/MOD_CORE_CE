"""authz-derived-ownership DO-2 — the effective-access resolver.

The Zanzibar *Expand* equivalent, and the surface an ISO 27001 / CMMC / FedRAMP /
TPN access review depends on: given a resource, WHO can reach it; given a user,
WHAT they can reach. Two properties are non-negotiable:

  * COMPLETE — it must never omit a principal who actually has access (an
    omission is an audit failure). Enumeration includes direct grants, folder
    inheritance, derived ownership, team grants, role-grant rows AND
    role-own-capabilities.
  * ENFORCEMENT-EQUIVALENT — every reported access is confirmed through the SAME
    engine `authorize()` / the cog inheritance walk uses, so a review can never
    permit or deny differently from live enforcement.

`who_can_access(..., exhaustive=True)` checks EVERY user in the tenant — the
dumb, unarguable proof an auditor can demand. The default bounded mode checks
only principals with a real path (a provable superset) for speed; the test suite
pins bounded == exhaustive so the fast path can never silently under-report.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (Capability, Cog, CustomRole, ResourcePermission, ResourceType,
                    User, Workflow, team_members_association)
from resource_permissions import (authorize, user_has_any_cog_grant_on,
                                   _derived_owner_allows)


def _caps_for(db: Session, resource_type: ResourceType) -> List[str]:
    return [r[0] for r in db.query(Capability.name).filter(
        Capability.resource_type == resource_type).all()]


def _cog_ancestor_ids(db: Session, cog_id: int) -> List[int]:
    """Ancestor folder ids of a cog, nearest-first. Cycle-safe."""
    out: List[int] = []
    visited = set()
    row = db.query(Cog.parent_id).filter(Cog.id == cog_id).first()
    parent = row[0] if row else None
    while parent and parent not in visited:
        visited.add(parent)
        out.append(parent)
        row = db.query(Cog.parent_id).filter(
            Cog.id == parent, Cog.deleted_at.is_(None)).first()
        parent = row[0] if row else None
    return out


def has_capability(db: Session, user: User, resource_type: ResourceType,
                   capability_name: str, resource_id: Optional[int]) -> bool:
    """Enforcement-equivalent check for one (user, capability, resource).

    For COG it reproduces `check_user_capability_on_cog`'s walk exactly — the
    NEAREST node the user has access to (a grant OR derived ownership) decides the
    capability via authorize() (direct-beats-inherited) — but for ANY canonical
    capability, not only the legacy-mapped names the wrapper accepts. For every
    other resource type it is authorize() directly. This is the single truth both
    the resolver and live enforcement share.
    """
    if resource_type == ResourceType.COG and resource_id is not None:
        node_id: Optional[int] = resource_id
        visited = set()
        while node_id is not None and node_id not in visited:
            visited.add(node_id)
            cog = db.query(Cog).filter(
                Cog.id == node_id, Cog.deleted_at.is_(None)).first()
            if cog is None:
                return False
            if (user_has_any_cog_grant_on(db, user, cog.id)
                    or _derived_owner_allows(db, user, ResourceType.COG, cog.id)):
                return authorize(db, user, ResourceType.COG, capability_name, cog.id)
            node_id = cog.parent_id
        return False
    return authorize(db, user, resource_type, capability_name, resource_id)


def _tenant_user_ids(db: Session, tenant_id: int) -> List[int]:
    return [r[0] for r in db.query(User.id).filter(User.tenant_id == tenant_id).all()]


def _resource_tenant(db: Session, resource_type: ResourceType,
                     resource_id: int) -> Optional[int]:
    if resource_type == ResourceType.COG:
        row = db.query(Cog.tenant_id).filter(Cog.id == resource_id).first()
    elif resource_type == ResourceType.WORKFLOW:
        row = db.query(Workflow.tenant_id).filter(Workflow.id == resource_id).first()
    else:
        return None
    return row[0] if row else None


def _bounded_candidate_user_ids(db: Session, resource_type: ResourceType,
                                resource_id: int, tenant_id: int) -> set:
    """Every user with a PLAUSIBLE path to the resource — a provable superset of
    those who actually have access. Confirmed individually afterwards, so an
    over-inclusive candidate is harmless; an OMITTED path would under-report, so
    every access mechanism is represented here:
    owner(s) of the resource and its ancestors · direct user grants on the
    resource, its ancestors, or wildcard · members of any team so granted ·
    holders of any role so granted (grant rows) · holders of any role whose
    own capabilities cover this resource type (the role-own-caps mechanism).
    """
    user_ids: set = set()

    # Nodes whose grants/ownership reach the resource (self + ancestors for cogs).
    if resource_type == ResourceType.COG:
        node_ids = [resource_id] + _cog_ancestor_ids(db, resource_id)
        for c in db.query(Cog).filter(Cog.id.in_(node_ids)).all():
            if c.owner_id:
                user_ids.add(c.owner_id)
    elif resource_type == ResourceType.WORKFLOW:
        node_ids = [resource_id]
        wf = db.query(Workflow).filter(Workflow.id == resource_id).first()
        if wf and wf.created_by_user_id:
            user_ids.add(wf.created_by_user_id)
    else:
        node_ids = [resource_id]

    # Grant rows on those nodes OR wildcard (resource_id NULL) for this type.
    perms = db.query(ResourcePermission).filter(
        ResourcePermission.resource_type == resource_type,
        ResourcePermission.is_active == True,  # noqa: E712
        ResourcePermission.revoked_at.is_(None),
        (ResourcePermission.resource_id.in_(node_ids))
        | (ResourcePermission.resource_id.is_(None)),
    ).all()

    team_ids, role_ids = set(), set()
    for p in perms:
        if p.user_id:
            user_ids.add(p.user_id)
        if getattr(p, "team_id", None):
            team_ids.add(p.team_id)
        if getattr(p, "role_id", None):
            role_ids.add(p.role_id)

    if team_ids:
        rows = db.execute(select(team_members_association.c.user_id).where(
            team_members_association.c.team_id.in_(team_ids))).all()
        user_ids.update(r[0] for r in rows)

    # Roles that carry this resource type in their OWN capabilities (role-only
    # mechanism), plus roles reached by a grant row above.
    role_own = db.query(CustomRole.id).join(CustomRole.capabilities).filter(
        CustomRole.deleted_at.is_(None),
        Capability.resource_type == resource_type,
    ).all()
    role_ids.update(r[0] for r in role_own)
    if role_ids:
        rows = db.query(User.id).filter(User.custom_role_id.in_(role_ids)).all()
        user_ids.update(r[0] for r in rows)

    # Tenant isolation: only same-tenant principals can hold access here.
    return {uid for uid in user_ids
            if db.query(User.id).filter(
                User.id == uid, User.tenant_id == tenant_id).first()}


def who_can_access(db: Session, resource_type: ResourceType, resource_id: int,
                   *, exhaustive: bool = False) -> List[Dict[str, Any]]:
    """WHO can reach this resource, and with which capabilities.

    Returns [{user_id, capabilities: [names]}], one entry per user with at least
    one capability. `exhaustive=True` checks every user in the tenant (the
    unarguable audit proof); the default bounded mode checks only plausible
    principals (a superset), and both are confirmed via `has_capability` so the
    two modes return identical results.
    """
    tenant_id = _resource_tenant(db, resource_type, resource_id)
    if tenant_id is None:
        return []
    caps = _caps_for(db, resource_type)
    if exhaustive:
        candidates = set(_tenant_user_ids(db, tenant_id))
    else:
        candidates = _bounded_candidate_user_ids(
            db, resource_type, resource_id, tenant_id)

    out: List[Dict[str, Any]] = []
    for uid in sorted(candidates):
        user = db.query(User).filter(User.id == uid).first()
        if user is None:
            continue
        held = [c for c in caps
                if has_capability(db, user, resource_type, c, resource_id)]
        if held:
            out.append({"user_id": uid, "capabilities": sorted(held)})
    return out


def what_can_user_reach(db: Session, user: User,
                        resource_type: Optional[ResourceType] = None
                        ) -> List[Dict[str, Any]]:
    """WHAT a user can reach: [{resource_type, resource_id, capabilities}].

    Candidate resources are the user's own footprint — owned, directly granted,
    team-granted, role-granted (rows), role-own-caps wildcards, and the
    descendants of any folder they can reach (forward inheritance) — each
    confirmed via `has_capability`. Optionally filtered to one resource type.
    """
    types = [resource_type] if resource_type else [
        ResourceType.COG, ResourceType.WORKFLOW]
    out: List[Dict[str, Any]] = []
    for rt in types:
        rids = _reachable_resource_ids(db, user, rt)
        caps = _caps_for(db, rt)
        for rid in sorted(r for r in rids if r is not None):
            held = [c for c in caps if has_capability(db, user, rt, c, rid)]
            if held:
                out.append({"resource_type": rt.value, "resource_id": rid,
                            "capabilities": sorted(held)})
    return out


def _reachable_resource_ids(db: Session, user: User,
                            resource_type: ResourceType) -> set:
    """The resource ids a user could plausibly reach (superset), same-tenant."""
    rids: set = set()
    # Owned.
    if resource_type == ResourceType.COG:
        rids.update(r[0] for r in db.query(Cog.id).filter(
            Cog.owner_id == user.id, Cog.tenant_id == user.tenant_id,
            Cog.deleted_at.is_(None)).all())
    elif resource_type == ResourceType.WORKFLOW:
        rids.update(r[0] for r in db.query(Workflow.id).filter(
            Workflow.created_by_user_id == user.id,
            Workflow.tenant_id == user.tenant_id,
            Workflow.deleted_at.is_(None)).all())
    # Granted directly / via team / via role (rows) → specific resource ids.
    team_ids = [r[0] for r in db.execute(select(
        team_members_association.c.team_id).where(
        team_members_association.c.user_id == user.id)).all()]
    conds = [ResourcePermission.user_id == user.id]
    if team_ids:
        conds.append(ResourcePermission.team_id.in_(team_ids))
    if getattr(user, "custom_role_id", None):
        conds.append(ResourcePermission.role_id == user.custom_role_id)
    from sqlalchemy import or_
    granted = db.query(ResourcePermission.resource_id).filter(
        ResourcePermission.resource_type == resource_type,
        ResourcePermission.is_active == True,  # noqa: E712
        ResourcePermission.revoked_at.is_(None),
        ResourcePermission.resource_id.isnot(None),
        or_(*conds),
    ).all()
    rids.update(r[0] for r in granted)
    # Forward inheritance: descendants of any folder cog the user can reach.
    if resource_type == ResourceType.COG:
        frontier = set(rids)
        seen = set()
        while frontier:
            batch = list(frontier)
            frontier = set()
            children = db.query(Cog.id).filter(
                Cog.parent_id.in_(batch), Cog.deleted_at.is_(None)).all()
            for (cid,) in children:
                if cid not in seen:
                    seen.add(cid)
                    rids.add(cid)
                    frontier.add(cid)
    return rids


# ---------------------------------------------------------------------------
# DO-5.2 — point-in-time reconstruction. "Who could access X as of date D",
# replayed from DURABLE temporal data: ResourcePermission.granted_at /
# revoked_at / expires_at for grants, and the ownership_transfer audit trail for
# ownership. This answers the AC-2 / ISO / TPN historical-review question — the
# grant + ownership TIMELINE — from records that are never mutated in place
# (revokes are soft; transfers are audited). Stated limits, not hidden: team
# MEMBERSHIP and folder INHERITANCE are not temporally recorded, so a returned
# team owner / grant is expanded against CURRENT membership, and inheritance is
# not reconstructed; the result is explicit about this in `caveats`.
# ---------------------------------------------------------------------------
from datetime import datetime, timezone  # noqa: E402


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _owner_as_of(db: Session, resource_type: ResourceType, resource_id: int,
                 as_of: datetime):
    """The owner principal (["user"|"team", id]) at `as_of`, replayed from the
    ownership_transfer audit trail. None when the resource was never transferred
    (the caller reports the current owner, which has held since creation)."""
    from models import PermissionAuditLog
    rows = db.query(PermissionAuditLog).filter(
        PermissionAuditLog.resource_type == resource_type.value,
        PermissionAuditLog.resource_id == resource_id,
    ).order_by(PermissionAuditLog.timestamp.asc()).all()
    transfers = [r for r in rows
                 if (r.audit_metadata or {}).get("event") == "ownership_transfer"]
    if not transfers:
        return None
    before = [r for r in transfers if _aware(r.timestamp) <= as_of]
    if before:
        return (before[-1].audit_metadata or {}).get("new_owner")
    # `as_of` predates the first recorded transfer — the owner then was that
    # transfer's old_owner.
    return (transfers[0].audit_metadata or {}).get("old_owner")


def _current_owner_descriptor(db: Session, resource_type: ResourceType,
                              resource_id: int):
    from models import Cog, PrincipalType, ResourceOwnership, Workflow
    row = db.query(ResourceOwnership).filter(
        ResourceOwnership.resource_type == resource_type,
        ResourceOwnership.resource_id == resource_id).first()
    if row is not None:
        return [row.principal_type.value, row.principal_id]
    if resource_type == ResourceType.COG:
        c = db.query(Cog).filter(Cog.id == resource_id).first()
        return ["user", c.owner_id] if (c and c.owner_id) else None
    if resource_type == ResourceType.WORKFLOW:
        w = db.query(Workflow).filter(Workflow.id == resource_id).first()
        return ["user", w.created_by_user_id] if (w and w.created_by_user_id) else None
    return None


def reconstruct_resource_access_as_of(db: Session, resource_type: ResourceType,
                                      resource_id: int, as_of: datetime) -> Dict[str, Any]:
    """DO-5.2 — who could access this resource at `as_of`.

    Returns {as_of, owner, grants:[{principal_type, principal_id, capability}],
    caveats:[...]}. `owner` is the owner at `as_of` (replayed from transfers, or
    the current owner if never transferred). `grants` are the direct/team/role
    grant rows temporally active at `as_of` (granted_at <= as_of, not revoked or
    expired before then), including wildcard grants. See module note for the
    membership/inheritance limits echoed in `caveats`.
    """
    as_of = _aware(as_of)
    owner = _owner_as_of(db, resource_type, resource_id, as_of)
    if owner is None:
        owner = _current_owner_descriptor(db, resource_type, resource_id)

    cap_name = {c.id: c.name for c in db.query(Capability).filter(
        Capability.resource_type == resource_type).all()}
    rows = db.query(ResourcePermission).filter(
        ResourcePermission.resource_type == resource_type,
        (ResourcePermission.resource_id == resource_id)
        | (ResourcePermission.resource_id.is_(None)),
        ResourcePermission.granted_at <= as_of,
        (ResourcePermission.revoked_at.is_(None))
        | (ResourcePermission.revoked_at > as_of),
        (ResourcePermission.expires_at.is_(None))
        | (ResourcePermission.expires_at > as_of),
    ).all()
    grants = []
    for r in rows:
        if r.capability_id not in cap_name:
            continue
        if r.user_id:
            p_type, p_id = "user", r.user_id
        elif r.team_id:
            p_type, p_id = "team", r.team_id
        elif r.role_id:
            p_type, p_id = "role", r.role_id
        else:
            continue
        grants.append({"principal_type": p_type, "principal_id": p_id,
                       "capability": cap_name[r.capability_id],
                       "wildcard": r.resource_id is None})

    return {
        "as_of": as_of.isoformat(),
        "owner": owner,
        "grants": grants,
        "caveats": [
            "Ownership and direct/team/role grants are reconstructed from durable "
            "temporal records (grant timestamps + the ownership_transfer audit "
            "trail).",
            "Team MEMBERSHIP and folder INHERITANCE are not temporally recorded: a "
            "returned team owner/grant reflects CURRENT membership, and inherited "
            "access is not reconstructed.",
        ],
    }

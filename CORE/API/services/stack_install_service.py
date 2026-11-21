"""GF.12 (launch-completion-megaplan, gf-commerce) — materialize an owned
catalog stack into the tenant's Persistent Stacks.

The purchase (closure fulfilment / free claim) hands out a
``TenantStackEntitlement``; this module turns it into an actual
``PersistentStack`` the tenant can deploy from the Persistent Stacks page
(the fit check happens there — install NEVER deploys).

Idempotent: the entitlement's ``installed_stack_id`` points at the tenant's
copy, so a second install returns the same stack with ``created: false``.
If that stack was reaped, the pointer is NULL (ON DELETE SET NULL) and a
fresh install creates a new one.

Compose provenance: the listing pins a ``StackPackage`` whose
``revision_id`` points at the exact ``PersistentStackRevision.compose_spec``
the publisher tested — that spec (never the live stack's current one) is
what the tenant installs.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from services import entitlement_source
try:
    from services import gov_mod_built_only as p5c
except ImportError:
    p5c = None

from product_names import themed_logger
logger = themed_logger(__name__, 'stacks')


class PurchaseRequired(Exception):
    """No active stack entitlement for this tenant/listing. The router
    maps this to a 402 ``{"code": "purchase_required"}`` body (same
    convention as ``catalog_service.PaymentRequired``)."""


def _dedup_name(db: Session, tenant_id: int, base: str) -> str:
    """`base`, or `base-2`, `base-3`, ... when the tenant already has a
    stack of that name (names are unique-per-tenant by the create flow's
    duplicate guard, which install must not trip)."""
    name = base
    n = 2
    while (db.query(models.PersistentStack.id)
           .filter(models.PersistentStack.tenant_id == tenant_id,
                   models.PersistentStack.name == name)
           .first()) is not None:
        name = f"{base}-{n}"
        n += 1
    return name


def _create_stack(db: Session, *, user: models.User, name: str,
                  compose_spec: dict, license_class: str) -> models.PersistentStack:
    """Create the tenant's copy through the EXISTING create-from-compose
    service (same _validate_compose gate as create / from-wizard /
    import-compose — a spec that fails validation 422s, never 500s).
    The function-local import keeps the routers.persistent_stacks package
    out of this module's import time (same convention as
    persistent_stacks_workers_fleet_service)."""
    from services.persistent_stacks_workers_fleet_service import create_stack as _svc_create
    from routers.persistent_stacks import _shared as _ps

    body = schemas.PersistentStackCreate(
        name=name,
        compose_spec=compose_spec,
    )
    _svc_create(db, body, user)
    # `_svc_create` returns the redacted DTO, not the ORM row — re-fetch by
    # (tenant, name) so the entitlement points at the real row.
    stack = (db.query(models.PersistentStack)
             .filter(models.PersistentStack.tenant_id == user.tenant_id,
                     models.PersistentStack.name == name)
             .first())
    if stack is None:  # pragma: no cover — create_stack just committed it
        raise HTTPException(status_code=500,
                            detail="stack create service committed no row")
    # `create_stack` already ran the stack-training-and-prewarm §2.1
    # best-effort characterization hook; only the install provenance audit
    # is added here.
    _ps._audit_stack(db, "STACK_INSTALLED", stack, user,
                     {"license_class": license_class})
    db.commit()
    return stack


def _create_install_secret(db: Session, *, tenant_id: int, name: str,
                           value: str, user: models.User) -> None:
    """13.5f — create the tenant secret an install declares but the tenant
    does not have yet, through the EXACT 13.5a storage path
    (``crud.create_or_update_secret``: encrypted DB row + Vault write,
    fail-closed). The creation is audited by NAME only — never the value.
    A Vault write failure is a 502 naming the secret, never its value."""
    import crud
    from utils.vault_client import VaultError

    try:
        crud.create_or_update_secret(
            db, secret_name=name, value=value, user=user,
            description="Created by store stack install (13.5f)")
    except VaultError as e:
        logger.warning("secret %r could not be stored (tenant %s): %s",
                       name, tenant_id, e)
        raise HTTPException(
            status_code=502,
            detail=f"secret {name!r} could not be stored (Vault unavailable)")
    try:
        db.add(models.AuditEvent(
            tenant_id=user.tenant_id,
            user_id=user.id,
            event_name="STACK_INSTALL_SECRET_CREATED",
            details={"secret_name": name, "tenant_id": tenant_id}))
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("install-secret audit failed for %r: %s", name, e)


def _ensure_install_secrets(db: Session, *, tenant_id: int, compose_spec: dict,
                            supplied: Optional[dict], user: models.User) -> list:
    """13.5f — gate the install on the package's declared secrets.

    Every REQUIRED secret (per
    ``services.stack_packages.package_required_secrets``) must be
    satisfiable before anything is created:
      * the tenant already has a live ``models.Secret`` row of that name →
        use it, nothing written;
      * else a value was supplied in the install request → create it via
        :func:`_create_install_secret` (the 13.5a storage path + audit);
      * else → collect it for the 422.

    Raises 422 ``{"missing_secrets": [names]}`` when any required secret
    cannot be satisfied — BEFORE any stack row is created. Optional (not
    required) secrets that are missing are fine: the deploy will simply
    leave them unresolved. Values are never logged or echoed."""
    from utils.paid_import import optional_paid
    _sp = optional_paid("services.stack_packages")
    if _sp is None:
        # CE — the paid package-metadata module is absent; nothing can be
        # declared, so the gate is vacuous (graceful no-op).
        return []
    supplied = supplied if isinstance(supplied, dict) else {}
    missing: list[str] = []
    created: list[str] = []
    for entry in _sp.package_required_secrets(compose_spec):
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        if not entry.get("required", True):
            continue
        existing = (db.query(models.Secret)
                    .filter(models.Secret.name == name,
                            models.Secret.tenant_id == tenant_id,
                            models.Secret.deleted_at.is_(None))
                    .first())
        if existing is not None:
            continue
        value = supplied.get(name)
        if isinstance(value, str) and value.strip():
            _create_install_secret(db, tenant_id=tenant_id, name=name,
                                   value=value, user=user)
            created.append(name)
        else:
            missing.append(name)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"missing_secrets": sorted(missing)})
    return created


def install_stack(
    db: Session,
    *,
    tenant_id: int,
    user: models.User,
    listing_id: int,
    name: Optional[str] = None,
    secrets: Optional[dict] = None,
) -> dict:
    """Install the tenant's entitled stack listing into Persistent Stacks.

    `secrets` (13.5f) is an optional ``{NAME: value}`` map for the tenant
    secrets the pinned package declares but the tenant does not have yet;
    already-present tenant secrets are used as-is and untouched.

    Returns ``{stack_id, name, created, needs_key, created_secrets}``.

    Raises:
        404 — listing/package not resolvable.
        :class:`PurchaseRequired` — no ACTIVE entitlement (revoked or
              expired rows count as not-owned); the router maps it to a
              402 ``{"code": "purchase_required"}`` body.
        403 — package ``license_class`` is 'internal' (never installable).
        409 — the pinned revision carries no compose spec.
        422 — the pinned compose spec fails the create validator, or a
              required secret has neither an existing tenant secret nor a
              supplied value (``{"missing_secrets": [names]}``).
    """
    listing = db.get(models.StackListing, listing_id)
    if listing is None or listing.deleted_at is not None:
        raise HTTPException(status_code=404, detail="stack listing not found")

    # FX.95 p5c (Q81=A) — gov/DoD profile (or the mod_built_only_enforced
    # opt-in) allows only MOD-built listings. Checked before anything is
    # created; an existing install is never deleted, only refused here.
    if p5c is not None:
        _p5c_reason = p5c.install_gate(db, tenant_id, listing, "stack_listing",
                                       actor_user_id=getattr(user, "id", None))
        if _p5c_reason is not None:
            raise HTTPException(status_code=403, detail=_p5c_reason)

    pkg = db.get(models.StackPackage, listing.package_id)
    if pkg is None or pkg.deleted_at is not None:
        raise HTTPException(status_code=404, detail="stack package not found")
    if (pkg.license_class or "redistributable") == "internal":
        raise HTTPException(
            status_code=403,
            detail="this stack is internal and cannot be installed")
    needs_key = (pkg.license_class or "") == "byo_key"

    ent = (db.query(models.TenantStackEntitlement)
           .filter(models.TenantStackEntitlement.tenant_id == tenant_id,
                   models.TenantStackEntitlement.listing_id == listing_id,
                   entitlement_source.active_filter(models.TenantStackEntitlement))
           .first())
    if ent is None:
        # CE auto-claim (FIX-CE-INSTALL-AUTO-CLAIM): MOD CE ships no billing,
        # so a FREE listing (same predicate claim_free uses in
        # services/catalog_service.py: `(price_cents or 0) > 0` is priced) is
        # granted on the spot with the SAME helper the claim route uses
        # (entitlement_source.grant, source "free" — idempotent: a repeat
        # call re-activates in place, never duplicates a row). The edition
        # probe is asked at call time with FAIL-TO-PAID (same pattern as
        # worker_management_service._seed_max_concurrent_tasks): any probe
        # error behaves as paid and keeps the existing refusal below.
        # A PRICED listing still refuses on CE — CE has no billing, so the
        # answer is the established CE "not available in this edition" shape
        # (utils/paid_import.require_paid's 402 detail), never a purchase
        # prompt. PAID editions are byte-identical: the probe is False,
        # nothing else runs, and the same PurchaseRequired fires.
        try:
            from utils.paid_import import is_ce_edition
            _ce = bool(is_ce_edition())
        except Exception:  # noqa: BLE001 — fail to PAID: keep the refusal
            _ce = False
        if _ce:
            if (listing.price_cents or 0) > 0:
                raise HTTPException(
                    status_code=402,
                    detail="Paid stack listings are not "
                           "available in MOD Community Edition.")
            entitlement_source.grant(
                db,
                tenant_id=tenant_id,
                kind="stack",
                item_id=listing_id,
                source="free",
                current_user=None,
                actor_user_id=getattr(user, "id", None),
            )
            ent = (db.query(models.TenantStackEntitlement)
                   .filter(models.TenantStackEntitlement.tenant_id == tenant_id,
                           models.TenantStackEntitlement.listing_id == listing_id,
                           entitlement_source.active_filter(models.TenantStackEntitlement))
                   .first())
        if ent is None:
            raise PurchaseRequired()

    # Idempotency: a live pointer wins over anything else.
    if ent.installed_stack_id is not None:
        existing = db.get(models.PersistentStack, ent.installed_stack_id)
        if existing is not None and existing.tenant_id == tenant_id:
            return {
                "stack_id": existing.id,
                "name": existing.name,
                "created": False,
                "needs_key": needs_key,
                "created_secrets": [],
            }
        # Pointer dangles (stack reaped) → fall through and re-install;
        # the FK is SET NULL so we simply record the new id below.

    rev = None
    if pkg.revision_id is not None:
        rev = db.get(models.PersistentStackRevision, pkg.revision_id)
    if rev is None or not isinstance(rev.compose_spec, dict) \
            or not rev.compose_spec.get("services"):
        raise HTTPException(
            status_code=409,
            detail="this stack version has no installable compose spec yet")
    compose_spec = rev.compose_spec

    # 13.5f — resolve the package's declared secrets BEFORE any row is
    # written: existing tenant secrets are used as-is, supplied values are
    # created through the 13.5a storage path, a missing required secret
    # 422s with {"missing_secrets": [names]}.
    created_secrets = _ensure_install_secrets(
        db, tenant_id=tenant_id, compose_spec=compose_spec,
        supplied=secrets, user=user)

    base_name = (name or "").strip() or listing.name or pkg.name
    final_name = _dedup_name(db, tenant_id, base_name)
    stack = _create_stack(db, user=user, name=final_name,
                          compose_spec=compose_spec,
                          license_class=pkg.license_class or "redistributable")

    ent.installed_stack_id = stack.id
    db.commit()
    logger.info("stack listing %s installed for tenant %s as stack %s",
                listing_id, tenant_id, stack.id)
    return {
        "stack_id": stack.id,
        "name": stack.name,
        "created": True,
        "needs_key": needs_key,
        "created_secrets": created_secrets,
    }


def upgrade_installed_stack(
    db: Session,
    *,
    tenant_id: int,
    user: models.User,
    stack_id: int,
    target_package_id: Optional[int] = None,
) -> dict:
    """GG.UI.5 — upgrade a tenant's already-installed stack to a newer
    published :class:`StackPackage` version of the SAME product (slug
    family) it was originally installed from.

    Writes the target package's frozen compose as a NEW revision of the
    tenant's EXISTING ``PersistentStack`` via ``services.
    persistent_stack_revisions.create_revision`` — the exact revision path
    ``PUT .../definition`` uses — and applies it to the live stack row the
    same way that router does. Never redeploys: the caller must hit
    ``POST .../deploy`` (or the Persistent Stacks UI's "Save & redeploy")
    to push the new revision to the worker.

    Limitation (flagged, not solved, here): ``install_stack`` has no
    separate store for tenant env overrides/config params — it writes the
    package's frozen compose_spec verbatim onto the new stack (see above).
    Because there is nothing to "preserve", an upgrade likewise writes the
    target package's compose verbatim; any manual edits a tenant made via
    the stack editor after install are NOT carried forward across an
    upgrade, same as they wouldn't be if the tenant re-installed from
    scratch.

    Returns ``{stack_id, from_version, to_version, redeploy_required: True}``.

    Raises:
        404 — stack not found for this tenant, or not installed from an
              active/owned stack entitlement.
        403 — target package's ``license_class`` is 'internal'.
        409 — already on the requested/latest version; ``target_package_id``
              resolves to the same or an older version than installed; the
              resolved target's revision carries no compose spec; or the
              resolved target has no published listing.
        422 — ``target_package_id`` belongs to a different product (slug).
    """
    stack = db.get(models.PersistentStack, stack_id)
    if stack is None or stack.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="stack not found")

    ent = (db.query(models.TenantStackEntitlement)
           .filter(models.TenantStackEntitlement.tenant_id == tenant_id,
                   models.TenantStackEntitlement.installed_stack_id == stack_id,
                   entitlement_source.active_filter(models.TenantStackEntitlement))
           .first())
    if ent is None:
        raise HTTPException(
            status_code=404,
            detail="stack was not installed from an owned stack listing")

    listing = db.get(models.StackListing, ent.listing_id)
    if listing is None or listing.deleted_at is not None:
        raise HTTPException(status_code=404, detail="stack listing not found")

    current_pkg = db.get(models.StackPackage, listing.package_id)
    if current_pkg is None or current_pkg.deleted_at is not None:
        raise HTTPException(status_code=404, detail="installed stack package not found")

    if target_package_id is not None:
        target_pkg = db.get(models.StackPackage, target_package_id)
        if target_pkg is None or target_pkg.deleted_at is not None:
            raise HTTPException(status_code=404, detail="target package not found")
        if target_pkg.slug != current_pkg.slug:
            raise HTTPException(
                status_code=422,
                detail="target package belongs to a different stack listing")
        if target_pkg.id == current_pkg.id or target_pkg.created_at <= current_pkg.created_at:
            raise HTTPException(
                status_code=409,
                detail="target package is not newer than the installed version")
    else:
        target_pkg = (db.query(models.StackPackage)
                     .filter(models.StackPackage.slug == current_pkg.slug,
                             models.StackPackage.deleted_at.is_(None))
                     .order_by(models.StackPackage.created_at.desc(),
                              models.StackPackage.id.desc())
                     .first())
        if target_pkg is None or target_pkg.id == current_pkg.id:
            raise HTTPException(status_code=409, detail="already on the latest version")

    target_listing = (db.query(models.StackListing)
                      .filter(models.StackListing.package_id == target_pkg.id,
                              models.StackListing.status == "published",
                              models.StackListing.deleted_at.is_(None))
                      .first())
    if target_listing is None:
        raise HTTPException(status_code=409, detail="target version is not published")

    if (target_pkg.license_class or "redistributable") == "internal":
        raise HTTPException(
            status_code=403,
            detail="this stack version is internal and cannot be installed")

    rev = None
    if target_pkg.revision_id is not None:
        rev = db.get(models.PersistentStackRevision, target_pkg.revision_id)
    if rev is None or not isinstance(rev.compose_spec, dict) \
            or not rev.compose_spec.get("services"):
        raise HTTPException(
            status_code=409,
            detail="this stack version has no installable compose spec yet")
    compose_spec = rev.compose_spec

    # Function-local imports for the same reason `_create_stack` keeps
    # `routers.persistent_stacks` out of this module's import time.
    from routers.persistent_stacks import _shared as _ps
    from services import persistent_stack_revisions as _psr
    from services import stack_compose_import as _sci

    storage_class = _sci.derive_stack_fields(compose_spec, None)["storage_class"]
    # Same validation gate `PUT .../definition` runs on every saved edit —
    # a package whose frozen compose can no longer pass the current
    # host-escape/admin-only-construct boundary 422s, never 500s.
    _ps._validate_compose(
        compose_spec,
        is_admin=bool(getattr(user, "is_platform_admin", False)),
        secret_prefix=f"tenants/{tenant_id}/stacks/{stack.name}/",
        tenant_id=tenant_id, db=db,
    )

    # Same lazy-materialize-then-append pattern `PUT .../definition` uses:
    # install never wrote a revision 1 (it only set stack.compose_spec), so
    # without this the upgrade's new revision would itself land as #1 and
    # the pre-upgrade compose would never appear in history.
    _psr.latest(db, stack.id) or _psr.materialize_initial(db, stack)

    new_rev = _psr.create_revision(
        db, stack,
        origin=_psr.ORIGIN_EDITED,
        author_user_id=user.id,
        compose_spec=compose_spec,
        storage_class=storage_class,
        message=f"stack upgrade: {current_pkg.version} -> {target_pkg.version}",
    )
    stack.compose_spec = compose_spec
    stack.accelerator_requirements = _ps._parse_accelerator_requirements(compose_spec) or None
    stack.egress_policy = _ps._parse_egress_policy(compose_spec) or None
    stack.secret_refs = _ps._parse_secret_refs(compose_spec) or None
    stack.tls_config = _ps._parse_tls_config(compose_spec) or None
    stack.storage_class = storage_class
    db.add(stack)

    # Record the new package/listing on the entitlement/stack link.
    ent.listing_id = target_listing.id
    db.add(ent)

    _ps._audit_stack(db, "STACK_UPGRADED", stack, user,
                     {"from_version": current_pkg.version,
                      "to_version": target_pkg.version,
                      "from_package_id": current_pkg.id,
                      "to_package_id": target_pkg.id,
                      "revision_no": new_rev.revision_no})
    db.commit()
    db.refresh(stack)
    logger.info("stack %s upgraded from %s to %s for tenant %s (revision %s)",
                stack.id, current_pkg.version, target_pkg.version, tenant_id,
                new_rev.revision_no)
    return {
        "stack_id": stack.id,
        "from_version": current_pkg.version,
        "to_version": target_pkg.version,
        "redeploy_required": True,
    }

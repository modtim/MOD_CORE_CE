"""Tenant-level CRUD operations - Tenant isolation and provisioning."""
from utils.pii_redaction import mask_email

import os
import logging
import time
from typing import Optional

from sqlalchemy.orm import Session
from datetime import datetime, timedelta

import models, schemas
from utils.paid_import import optional_paid
from utils.keycloak_client import create_tenant_group

_credit_mgr_mod = optional_paid("utils.credit_manager")
CreditManager = _credit_mgr_mod.CreditManager if _credit_mgr_mod else None

# Setup logging
logger = logging.getLogger(__name__)

# Check if debug mode is enabled (may expose sensitive data)
from services.debug_mode import is_debug_mode


# -----------------------------------------------------------------------------
# Default system roles seeded for every tenant.
# -----------------------------------------------------------------------------
# Sentinel meaning "all capabilities" — resolved at seed time to the full
# Capability table. Used for the Admin role.
_ALL_CAPABILITIES = object()

# Ordered list of (name, description, capabilities-spec). Capabilities-spec is
# either _ALL_CAPABILITIES (resolves to every Capability row) or a list of
# (ResourceType, capability_name) tuples.
SYSTEM_ROLE_SPECS = [
    (
        "Admin",
        "Full access to all features and settings.",
        _ALL_CAPABILITIES,
    ),
    (
        "Standard User",
        "Can create and run workflows, manage secrets, and collaborate.",
        # Lazy import inside the function body that uses this; we can't reference
        # models.ResourceType at module-import time because models imports back
        # into crud and we'd hit a circular import. So this list is intentionally
        # a tuple of (string_resource_type, capability_name) — _ensure_system_roles
        # below resolves the ResourceType enum at call time.
        #
        # PRIVATE-BY-DEFAULT: a role-own-capability is tenant-WIDE — it grants the
        # capability on EVERY resource of that type in the tenant, unconditioned by
        # any per-resource grant. So per-RESOURCE capabilities (view/edit/execute/
        # view_runs/share/download_original on a specific workflow or folder) must
        # NOT live here, or per-resource sharing, revoke and downgrade cannot govern
        # access (a role holder keeps access a revoke can't remove; live: qa-user
        # read/edited/executed qa-owner's un-shared workflow via WORKFLOW:edit/
        # execute/view_runs still carried here). Owners still reach their OWN
        # resources via derived ownership, and WORKFLOW:create is genuinely
        # tenant-global (making a new workflow needs no existing resource), so it
        # stays. Everything else is governed per-resource by grants.
        [
            ("SECRET", "view"), ("SECRET", "create"), ("SECRET", "edit"),
            ("SECRET", "use_in_workflow"),
            ("USER", "view"), ("USER", "invite"),
            ("TEAM", "view"),
            ("WORKFLOW", "create"),
        ],
    ),
    (
        "Guest",
        "Read-only access to view resources.",
        [
            ("USER", "view"),
            ("TEAM", "view"),
            ("SECRET", "view"),
            ("COG", "view"), ("COG", "download_original"),
            ("WORKFLOW", "view"), ("WORKFLOW", "view_runs"),
        ],
    ),
    (
        "External Collaborator",
        "Limited access for external users. Can view shared resources.",
        [
            ("USER", "view"),
            ("TEAM", "view"),
            ("COG", "view"), ("COG", "download_original"),
        ],
    ),
]

SYSTEM_ROLE_NAMES = frozenset(name for name, _, _ in SYSTEM_ROLE_SPECS)


def ensure_tenant_system_roles(db: Session, tenant_id: int) -> dict:
    """Idempotently seed the default system roles for a tenant.

    Returns a dict mapping role name -> CustomRole instance for all 4 system
    roles (whether newly created or already present). Existing rows that
    match a system role by name are normalized: `is_system_role` is set to
    True so the API can lock them against rename/delete.

    Safe to call repeatedly; missing roles are created with full capability
    sets, existing roles are reused as-is (capability lists are not rewritten,
    so per-tenant customizations are preserved).
    """
    all_caps = db.query(models.Capability).all()
    cap_lookup = {(cap.resource_type, cap.name): cap for cap in all_caps}

    # 2400LR-userview — seed the Capability catalog BEFORE resolving the role
    # specs. The role bootstrap runs before main.py's `seed_capabilities`
    # lifespan task (database.py:723 + main.py:4002 seed_ce_admin_user come
    # before main.py:509), so on a fresh CE install Admin's `_ALL_CAPABILITIES`
    # resolved against an EMPTY catalog and the tenant Admin carried ZERO caps
    # — authorize() (resource_permissions.py:315) then has no USER:view and
    # GET /v1/users 403s. Only fires when the catalog is short of a row some
    # system-role spec declares, so a healthy DB pays nothing. Idempotent
    # product path (seed_capabilities.seed_all_capabilities); the rows it adds
    # are tracked so a role that already exists but was resolved against the
    # short catalog picks exactly those up below (never an operator's removal).
    def _cap_key(cap):
        return (getattr(cap.resource_type, "name", str(cap.resource_type)), cap.name)

    pre_keys = {_cap_key(c) for c in all_caps}

    def _declared_short():
        for _, _, spec in SYSTEM_ROLE_SPECS:
            if spec is _ALL_CAPABILITIES:
                if not all_caps:
                    return True
                continue
            for rt_str, cap_name in spec:
                if (rt_str, cap_name) not in pre_keys:
                    return True
        return False

    if _declared_short():
        try:
            from seed_capabilities import seed_all_capabilities as _seed_caps
            _created, _updated = _seed_caps(db)
            logger.info(
                "ensure_tenant_system_roles: pre-seeded the Capability catalog "
                f"before role specs ({_created} new, {_updated} updated)"
            )
        except Exception as e:
            logger.warning(f"ensure_tenant_system_roles: capability catalog pre-seed failed: {e}")
        all_caps = db.query(models.Capability).all()
        cap_lookup = {(cap.resource_type, cap.name): cap for cap in all_caps}

    # Capability rows this call added to the catalog: the ONLY capability
    # shortfalls a pre-existing system role may be topped up for — they were
    # unresolvable when the role was created, so no operator removed them.
    gained_keys = {_cap_key(c) for c in all_caps} - pre_keys


    def _resolve_caps(spec):
        if spec is _ALL_CAPABILITIES:
            return list(all_caps)
        out = []
        for resource_type_str, cap_name in spec:
            rt = getattr(models.ResourceType, resource_type_str, None)
            if rt is None:
                continue
            cap = cap_lookup.get((rt, cap_name))
            if cap is not None:
                out.append(cap)
        return out

    result = {}
    for name, description, cap_spec in SYSTEM_ROLE_SPECS:
        existing = db.query(models.CustomRole).filter(
            models.CustomRole.tenant_id == tenant_id,
            models.CustomRole.name == name,
            models.CustomRole.deleted_at.is_(None),
        ).first()
        if existing is not None:
            if not existing.is_system_role:
                existing.is_system_role = True
            # Self-heal the seed-time capability shortfall (2400LR-userview).
            # A system role created BEFORE the Capability catalog was seeded
            # resolved its spec against an empty (or only partially seeded)
            # table, so it carries ZERO or a PARTIAL capability set and grants
            # nothing / less than declared — on a fresh CE boot the Admin
            # role's _ALL_CAPABILITIES resolves to [] before seed_capabilities
            # runs, so authorize() has no user:view and GET /v1/users 403s.
            # The old ZERO-only heal missed the PARTIAL case (a role that got
            # some caps because the catalog was mid-seed), which is exactly
            # what a fresh CE install hits when the catalog seed and the role
            # bootstrap interleave. Top up the capabilities the role's OWN
            # spec declares that are missing now: never remove an existing
            # grant, never widen beyond the declared set, never touch
            # non-system roles. Idempotent (a complete role adds nothing).
            if existing.is_system_role:
                declared = _resolve_caps(cap_spec)
                if not existing.capabilities:
                    # ZERO heal (unchanged): an empty system role gets its
                    # full declared set.
                    if declared:
                        existing.capabilities = declared
                elif cap_spec is _ALL_CAPABILITIES:
                    # FULL-SPEC heal (2400LR-userview, R01b): the Admin role's
                    # spec is the whole catalog, so a PARTIAL Admin is a seed
                    # shortfall by definition — there is no narrower declared
                    # set an operator could have trimmed to, and the role is
                    # locked against rename/delete, so a missing row is not a
                    # meaningful customisation. Converge to EVERY catalog row,
                    # not only the ones this call added: on an already-seeded
                    # DB gained_keys is empty, which left the partial Admin
                    # without USER:view and GET /v1/users 403'd. Add-only, so
                    # nothing is ever removed; idempotent once complete.
                    have = {c.id for c in existing.capabilities}
                    missing = [c for c in declared if c.id not in have]
                    if missing:
                        existing.capabilities = list(existing.capabilities) + missing
                else:
                    # PARTIAL top-up for an EXPLICITLY-declared system role:
                    # only capabilities this call just added to the catalog —
                    # they did not exist when the role was created, so no
                    # operator removed them. Anything else the role lacks is
                    # an intentional customisation of a narrow spec: never
                    # re-add, never widen beyond the declared set.
                    have = {c.id for c in existing.capabilities}
                    missing = [
                        c for c in declared
                        if c.id not in have and _cap_key(c) in gained_keys
                    ]
                    if missing:
                        existing.capabilities = list(existing.capabilities) + missing
            result[name] = existing
            continue

        role = models.CustomRole(
            name=name,
            description=description,
            tenant_id=tenant_id,
            is_system_role=True,
            capabilities=_resolve_caps(cap_spec),
        )
        db.add(role)
        result[name] = role

    db.commit()
    for r in result.values():
        db.refresh(r)
    return result


def _provision_tenant_defaults(db: Session, tenant, granted_by_user_id: Optional[int] = None):
    """Provision the default resources a tenant needs to be usable.

    Shared by BOTH tenant-creation paths — the signup path
    (create_tenant_and_parent_user) and the admin path
    (create_tenant_with_default_roles) — so both yield identically-provisioned
    tenants. Three steps:

      1. Harbor project + robot (NON-FATAL — see below).
      2. Copy every system Node Definition (tenant_id IS NULL) to the tenant.
      3. Attach a free-tier Subscription and initialize credits.

    Node-copy + subscription are the load-bearing parts. Idempotent: node-copy
    skips names the tenant already has, subscription skips if one is active.

    `granted_by_user_id` is the actor performing an admin-initiated create
    (None for self-serve signup); reserved for audit attribution of the
    provisioned defaults.
    """
    # --- 1. Tenant Harbor project + per-tenant robot account (NON-FATAL) ---
    # The tenant-Harbor TLS path is currently broken, and a tenant is fully
    # usable without a registry (only custom node pushes need it, and it can be
    # reprovisioned later), so any failure here logs a warning and continues
    # rather than aborting tenant creation. The only clean skip is the
    # ENABLE_TENANT_HARBOR flag being off.
    # GS.PSR.8 — harbor.tenant_harbor_enabled via the platform-settings
    # registry (DB > env > True), read per tenant-create.
    from utils.paid_import import optional_paid
    _thc = optional_paid("utils.tenant_harbor_client")
    if _thc is not None and _thc.tenant_harbor_enabled(db):
        try:
            TenantHarborClient, project_name_for_tenant = (
                _thc.TenantHarborClient, _thc.project_name_for_tenant
            )
            from utils.vault_client import approle_login, write_kv_v2

            # Sentinel handling: Plan.max_storage_gb uses 10^15 to mean
            # "unlimited"; pass -1 to Harbor so its own unlimited path kicks in.
            _UNLIMITED_SENTINEL_GB = 1_000_000_000_000_000
            quota_bytes = -1
            try:
                _free_plan = db.query(models.Plan).filter(
                    models.Plan.is_free_tier == True
                ).first()
                if _free_plan and _free_plan.max_storage_gb:
                    if _free_plan.max_storage_gb >= _UNLIMITED_SENTINEL_GB:
                        quota_bytes = -1
                    else:
                        quota_bytes = int(_free_plan.max_storage_gb) * 1024 * 1024 * 1024
            except Exception as e:
                logger.warning(
                    "Could not resolve free-plan storage for tenant %s — "
                    "falling back to unlimited registry quota: %s",
                    tenant.id, e,
                )

            harbor = TenantHarborClient()
            harbor.create_project(tenant.id, storage_quota_bytes=quota_bytes)
            # prevent_vul=False: tenant images inherit unfixable Debian CVEs;
            # keep scans on (severity=Critical) but leave the pull-gate off.
            harbor.set_project_vulnerability_policy(
                tenant.id, severity="Critical", prevent_vul=False,
            )

            robot = harbor.create_robot(
                tenant.id,
                name=f"mod-push-{tenant.id}",
                actions=["push", "pull"],
            )

            role_id = os.getenv("OPENBAO_APPROLE_ROLE_ID") or os.getenv("VAULT_APPROLE_ROLE_ID", "")
            secret_id = os.getenv("OPENBAO_APPROLE_SECRET_ID") or os.getenv("VAULT_APPROLE_SECRET_ID", "")
            token = approle_login(role_id=role_id, secret_id=secret_id)
            registry_url = (
                os.getenv("TENANT_HARBOR_EXTERNAL_URL")
                or os.getenv("TENANT_HARBOR_INTERNAL_URL")
                or "http://tenant-harbor-core:8080"
            )
            write_kv_v2(
                f"tenants/{tenant.id}/tenant_harbor",
                token=token,
                data={
                    "username": robot.username,
                    "password": robot.password,
                    "registry": registry_url,
                    "project": project_name_for_tenant(tenant.id),
                },
            )
            logger.info(
                "Provisioned tenant registry project + robot for tenant %s", tenant.id
            )
        except Exception as e:
            # NON-FATAL: do not abort tenant creation on a Harbor failure.
            logger.warning(
                "Tenant registry provisioning failed for tenant %s (non-fatal): %s",
                tenant.id, e,
            )

    # --- 2. Copy system Node Definitions to the new tenant ---
    logger.info(f"Provisioning system Node Definitions to tenant {tenant.id}...")
    system_nodes = db.query(models.NodeDefinition).filter(
        models.NodeDefinition.tenant_id.is_(None)
    ).all()

    from utils.node_cost import tier_columns_available
    copy_tiers = tier_columns_available(db)
    from services.node_scanner import seeded_gpu_variants as _seeded_gpu_variants

    for system_node in system_nodes:
        # Check if this node already exists for this tenant (might exist from retry)
        existing_node = db.query(models.NodeDefinition).filter(
            models.NodeDefinition.name == system_node.name,
            models.NodeDefinition.tenant_id == tenant.id
        ).first()

        if not existing_node:
            tier_kwargs = {}
            if copy_tiers:
                tier_kwargs = {
                    "cost_credits_by_tier": dict(system_node.cost_credits_by_tier or {}),
                    "cost_basis": system_node.cost_basis,
                }
            # Create a copy of the system node for this tenant
            tenant_node = models.NodeDefinition(
                name=system_node.name,
                tenant_id=tenant.id,
                description=system_node.description,
                type=system_node.type,
                executable_path=system_node.executable_path,
                input_schema=system_node.input_schema,
                output_schema=system_node.output_schema,
                configuration_schema=system_node.configuration_schema,
                custom_metadata=system_node.custom_metadata,
                process_type=system_node.process_type,
                version=system_node.version,
                cost_in_credits=system_node.cost_in_credits,
                node_kind=system_node.node_kind,
                batch_mode=system_node.batch_mode,
                accepted_formats=system_node.accepted_formats,
                # Seed-backed: a NULL system value must not leave a CUDA-coded
                # kind variant-less (it would route to the CPU queue).
                gpu_variants=_seeded_gpu_variants(
                    system_node.node_kind, system_node.gpu_variants),
                **tier_kwargs,
            )
            db.add(tenant_node)
        elif not existing_node.gpu_variants:
            # Reseed a pre-existing tenant copy whose value is NULL/empty
            # (never overwrites a non-NULL value).
            _filled = _seeded_gpu_variants(
                system_node.node_kind, system_node.gpu_variants)
            if _filled:
                existing_node.gpu_variants = _filled

    if system_nodes:
        db.commit()
        logger.info(f"Provisioned {len(system_nodes)} Node Definitions to tenant {tenant.id}")
    else:
        logger.warning("No system Node Definitions found to provision")

    # --- 3. Assign free-tier (Sandbox) plan + initialize credits ---
    existing_subscription = db.query(models.Subscription).filter(
        models.Subscription.tenant_id == tenant.id,
        models.Subscription.status == "active"
    ).first()

    if not existing_subscription:
        # Get the free tier plan
        free_plan = db.query(models.Plan).filter(
            models.Plan.is_free_tier == True
        ).first()

        if free_plan:
            # Check if plan has a free trial period
            trial_days = getattr(free_plan, 'free_trial_days', 0) or 0
            is_trial = trial_days > 0
            now = datetime.utcnow()

            subscription = models.Subscription(
                tenant_id=tenant.id,
                plan_id=free_plan.id,
                status="trialing" if is_trial else "active",
                is_trial=is_trial,
                trial_ends_at=(now + timedelta(days=trial_days)) if is_trial else None,
                current_period_start=now,
                current_period_ends_at=now + timedelta(days=max(trial_days, 30)),
                stripe_subscription_id=None,
                stripe_customer_id=None
            )
            db.add(subscription)
            db.commit()
            db.refresh(subscription)

            # 26.3: the MOD Core starter package is the new tenant's palette.
            from services.starter_package import grant_starter_nodes
            grant_starter_nodes(db, tenant.id)
            db.commit()

            # Initialize credits for the tenant (CE: no billing — skip, unlimited credits)
            if CreditManager is not None:
                CreditManager.initialize_credits_for_new_subscription(
                    db=db,
                    tenant_id=tenant.id,
                    subscription_id=subscription.id
                )

                # One-time welcome bonus so new signups can explore the platform in
                # their first month (configurable; default 2000 credits).
                try:
                    bonus = CreditManager.grant_signup_welcome_bonus(db, tenant.id)
                    if bonus:
                        logger.info(f"Granted {bonus} welcome-bonus credits to new tenant {tenant.id}")
                except Exception:
                    logger.exception(f"Welcome-bonus grant failed for tenant {tenant.id} (non-fatal)")

            logger.info(f"Assigned tenant {tenant.id} to '{free_plan.name}' plan with {free_plan.credits_per_month} credits")
        else:
            logger.error(f"Error: No free tier plan found. Aborting tenant {tenant.id} creation.")
            raise Exception("System configuration error: No free tier plan available to assign to new tenant.")


def create_tenant_and_parent_user(
    db: Session,
    user_details: schemas.UserCreate
) -> models.User:
    """
    Creates a new Tenant and then creates the first User for that tenant,
    making them the parent user (parent_user_id is None).

    ALSO provisions all system-level Node Definitions to the new tenant.

    WS2.1 — CE (open-core) editions are single-tenant: in the CE carve the
    user is assigned to the DEFAULT_TENANT_ID tenant (seeded at startup)
    instead of creating a per-customer tenant. SaaS and licensed on-prem
    deployments keep the per-customer path below, unchanged.
    """
    # 0. CE single-tenant routing (no-op outside the CE carve).
    try:
        from services.node_scanner import _is_ce_edition, default_tenant_id
        if _is_ce_edition():
            _default_tenant_id = default_tenant_id()
            ce_tenant = db.query(models.Tenant).filter(
                models.Tenant.id == _default_tenant_id
            ).first()
            if ce_tenant is None:
                # Startup seed not run yet (or its commit raced this one) —
                # heal in-process so signup still succeeds. Soft: create-if-
                # missing only, never touch an existing row.
                logger.warning(
                    "CE default tenant %s missing at signup; healing in-process",
                    _default_tenant_id,
                )
                try:
                    from services.node_scanner import ensure_default_tenant
                    ensure_default_tenant(db)
                except Exception:
                    db.rollback()
                ce_tenant = db.query(models.Tenant).filter(
                    models.Tenant.id == _default_tenant_id
                ).first()
                if ce_tenant is None:
                    raise
            system_roles = ensure_tenant_system_roles(db, ce_tenant.id)
            admin_role = system_roles["Admin"]
            existing_user = db.query(models.User).filter(
                models.User.keycloak_user_id == user_details.keycloak_user_id
            ).first()
            if existing_user:
                db_user = existing_user
            else:
                db_user = models.User(
                    keycloak_user_id=user_details.keycloak_user_id,
                    username=user_details.username,
                    email=user_details.email,
                    first_name=user_details.first_name,
                    last_name=user_details.last_name,
                    tenant_id=ce_tenant.id,
                    parent_user_id=None,
                    status=models.UserStatus.ACTIVE,
                    onboarding_complete=True,
                    custom_role_id=admin_role.id,
                )
                db.add(db_user)
                db.commit()
                db.refresh(db_user)
                # Assign user to s3_tenant_admin Keycloak group (dual-read alias:
                # resolves minio_tenant_admin → s3_tenant_admin in legacy realms)
                try:
                    from utils.keycloak_client import assign_user_to_group
                    if db_user.keycloak_user_id:
                        assign_user_to_group(db_user.keycloak_user_id, "s3_tenant_admin")
                except Exception as e:
                    if is_debug_mode():
                        logger.warning(f"Failed to assign user to s3_tenant_admin group: {e}")
                    else:
                        logger.warning("Failed to assign user to s3_tenant_admin group")
            _provision_tenant_defaults(db, ce_tenant)
            return db_user
    except Exception:
        # Fail open to the per-customer path: a CE-signup heal hiccup must
        # not 500 signup; the user lands in their own tenant, same as today.
        db.rollback()
        logger.warning(
            "CE default-tenant routing failed; falling back to per-customer tenant",
            exc_info=True,
        )

    # 1. Check if a tenant with this name already exists
    tenant_name = f"{user_details.email}-tenant"
    db_tenant = db.query(models.Tenant).filter(models.Tenant.name == tenant_name).first()

    # 2. If the tenant does not exist, create it
    if not db_tenant:
        # GT.D7 — single-tenant guard (CE / non-multi-tenant tiers): refuse to
        # mint a new per-customer tenant once the edition's quota is reached.
        # The CE branch above (which assigns signups to the existing default
        # tenant) never reaches here, so the bootstrap/default tenant is always
        # creatable. Idempotent retries (db_tenant already exists) skip the
        # guard. Raises TenantLimitError (a ServiceError) which the onboarding
        # wrapper maps to a 409.
        from services.tenant_limits import assert_can_create_tenant
        assert_can_create_tenant(db)

        db_tenant = models.Tenant(name=tenant_name)
        db.add(db_tenant)
        db.flush()  # allocate the PK before wrapping (per-tenant Vault key name)
        # 1788j CE carve: services.key_provider is paid/denied in CE. The CE
        # default is the shipped local tenant-key module (same
        # generate_and_wrap_tenant_key API, still an encrypted envelope) —
        # encryption is NEVER skipped and plaintext is NEVER written. SaaS
        # keeps the paid Vault/PKCS#11/KMIP provider.
        from services import tenant_key_local
        _key_provider = optional_paid("services.key_provider") or tenant_key_local
        _tenant_key, db_tenant.encryption_key = _key_provider.generate_and_wrap_tenant_key(
            db_tenant.id, db=db)
        db.commit()
        db.refresh(db_tenant)

        # Provision MinIO bucket for the new tenant — bounded retry so a
        # transient Vault-init race at first boot doesn't leave the tenant
        # without storage.
        _bucket_ok = False
        for _i in range(3):
            try:
                from utils.minio_client import MinioClient
                minio_client = MinioClient(tenant_id=db_tenant.id)
                minio_client.ensure_bucket_exists()
                logger.info(
                    f"Created MinIO bucket '{minio_client.bucket_name}' "
                    f"for tenant {db_tenant.id}")
                _bucket_ok = True
                break
            except Exception as e:
                if is_debug_mode():
                    logger.warning(
                        "Failed to create MinIO bucket for tenant %d "
                        "(attempt %d/3): %s", db_tenant.id, _i + 1, e)
                else:
                    logger.warning(
                        "Failed to create MinIO bucket for tenant %d "
                        "(attempt %d/3)", db_tenant.id, _i + 1)
                time.sleep(2)
        if not _bucket_ok:
            logger.warning(
                "Failed to create MinIO bucket for tenant %d", db_tenant.id)

        # Create Keycloak group for tenant
        try:
            group_id = create_tenant_group(db_tenant.id, db_tenant.name)
            if group_id:
                logger.info(f"Created Keycloak group for tenant {db_tenant.id}")
            else:
                logger.warning(f"Failed to create Keycloak group for tenant {db_tenant.id}")
        except Exception as e:
            if is_debug_mode():
                logger.warning(f"Failed to create Keycloak group for tenant {db_tenant.id}: {e}")
            else:
                logger.warning(f"Failed to create Keycloak group for tenant {db_tenant.id}")

    # 3. Seed system roles (idempotent — safe on retry or re-entry).
    system_roles = ensure_tenant_system_roles(db, db_tenant.id)
    admin_role = system_roles["Admin"]

    # 4. Create the user
    existing_user = db.query(models.User).filter(
        models.User.keycloak_user_id == user_details.keycloak_user_id
    ).first()

    if existing_user:
        db_user = existing_user
    else:
        db_user = models.User(
            keycloak_user_id=user_details.keycloak_user_id,
            username=user_details.username,
            email=user_details.email,
            first_name=user_details.first_name,
            last_name=user_details.last_name,
            tenant_id=db_tenant.id,
            parent_user_id=None,
            status=models.UserStatus.ACTIVE,
            onboarding_complete=True,
            custom_role_id=admin_role.id,
        )
        db.add(db_user)
        db.commit()
        db.refresh(db_user)

        # Assign user to s3_tenant_admin Keycloak group (dual-read alias:
        # resolves minio_tenant_admin → s3_tenant_admin in legacy realms)
        try:
            from utils.keycloak_client import assign_user_to_group
            if db_user.keycloak_user_id:
                assign_user_to_group(db_user.keycloak_user_id, "s3_tenant_admin")
        except Exception as e:
            if is_debug_mode():
                logger.warning(f"Failed to assign user to s3_tenant_admin group: {e}")
            else:
                logger.warning("Failed to assign user to s3_tenant_admin group")

    # 6. Provision node definitions, Harbor project, and the free-tier
    # subscription + credits (shared with the admin tenant-create path).
    _provision_tenant_defaults(db, db_tenant)

    return db_user


def create_tenant_with_default_roles(
    db: Session,
    tenant_data: schemas.TenantCreate
) -> models.Tenant:
    """
    Creates a new tenant with default roles (Admin, Standard User, Guest, External Collaborator).
    This is used by platform admins to create new tenant organizations.
    """
    # GT.D7 — single-tenant guard (CE / non-multi-tenant tiers): refuse to
    # mint another tenant once the edition's quota is reached. The first
    # tenant is always creatable (count == 0 short-circuit in the guard).
    # Raises TenantLimitError (a ServiceError) which the calling router maps
    # to a 4xx.
    from services.tenant_limits import assert_can_create_tenant
    assert_can_create_tenant(db)

    # Create the tenant, then generate + wrap its encryption key (PK must
    # be allocated first for the per-tenant Vault transit key name).
    db_tenant = models.Tenant(
        name=tenant_data.name,
        credit_balance=tenant_data.credit_balance,
    )
    db.add(db_tenant)
    db.flush()
    # 1788j CE carve: same as create_tenant_and_parent_user above — paid
    # key_provider absent -> shipped local tenant-key module (encrypted
    # envelope, no BYOK/Vault transit). Encryption never skipped.
    from services import tenant_key_local
    _key_provider = optional_paid("services.key_provider") or tenant_key_local
    _tenant_key, db_tenant.encryption_key = _key_provider.generate_and_wrap_tenant_key(
        db_tenant.id, db=db)
    db.commit()
    db.refresh(db_tenant)

    # Provision MinIO bucket for the new tenant — bounded retry so a
    # transient Vault-init race at first boot doesn't leave the tenant
    # without storage.
    _bucket_ok = False
    for _i in range(3):
        try:
            from utils.minio_client import MinioClient
            minio_client = MinioClient(tenant_id=db_tenant.id)
            minio_client.ensure_bucket_exists()
            logger.info(
                f"Created MinIO bucket '{minio_client.bucket_name}' "
                f"for tenant {db_tenant.id}")
            _bucket_ok = True
            break
        except Exception as e:
            if is_debug_mode():
                logger.warning(
                    "Failed to create MinIO bucket for tenant %d "
                    "(attempt %d/3): %s", db_tenant.id, _i + 1, e)
            else:
                logger.warning(
                    "Failed to create MinIO bucket for tenant %d "
                    "(attempt %d/3)", db_tenant.id, _i + 1)
            time.sleep(2)
    if not _bucket_ok:
        logger.warning(
            "Failed to create MinIO bucket for tenant %d", db_tenant.id)

    # Create Keycloak group for tenant
    try:
        group_id = create_tenant_group(db_tenant.id, db_tenant.name)
        if group_id:
            logger.info(f"Created Keycloak group for tenant {db_tenant.id}")
        else:
            logger.warning(f"Failed to create Keycloak group for tenant {db_tenant.id}")
    except Exception as e:
        if is_debug_mode():
            logger.warning(f"Failed to create Keycloak group for tenant {db_tenant.id}: {e}")
        else:
            logger.warning(f"Failed to create Keycloak group for tenant {db_tenant.id}")

    # Seed system roles (idempotent).
    ensure_tenant_system_roles(db, db_tenant.id)

    # Provision the same defaults signup gives a tenant: system Node Definition
    # copies, a Harbor project (non-fatal), and a free-tier subscription +
    # credits. Without this an admin-created tenant would have ZERO nodes and no
    # subscription.
    _provision_tenant_defaults(db, db_tenant, granted_by_user_id=None)

    db.refresh(db_tenant)
    return db_tenant


def trigger_invitation_dag(email: str, token: str, inviter_name: str):
    """
    Logs the intent to send an invitation email. The actual send happens
    via utils.email_sender — this function is a hook point for audit
    logging at the dispatch call site.
    """
    from logging import getLogger
    logger = getLogger(__name__)

    # Placeholder — real invitation-email send happens via the email
    # service. Logged here so audit + manual testing can confirm the
    # call site fires.
    logger.info(f"=== invitation-email dispatch ===")
    logger.info(f"To: {mask_email(email)}")
    logger.info(f"Inviter: {inviter_name}")
    logger.info(f"Token: ***REDACTED***")
    logger.info(f"=== end ===")

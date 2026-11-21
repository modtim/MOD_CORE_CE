# CORE/models.py

import enum
import json
from datetime import datetime
from typing import List, Optional
import uuid

from sqlalchemy import (
    Numeric,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    and_,
    or_,
    select,
    text,
    TypeDecorator,
    JSON,
    LargeBinary
)
from sqlalchemy.dialects.postgresql import JSONB as PostgresqlJSONB, ARRAY as PG_ARRAY
from sqlalchemy.orm import declarative_base, relationship, foreign, declared_attr, validates, deferred
from sqlalchemy.dialects.postgresql import UUID


# --- Custom Type for JSONB ---
class JSONB(TypeDecorator):
    """Platform-independent JSONB type.

    Uses PostgreSQL's JSONB type and falls back to JSON for other
    database backends.
    """
    impl = JSON

    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(PostgresqlJSONB())
        else:
            return dialect.type_descriptor(JSON())

    cache_ok = True


# --- Custom Type for ARRAY ---
class SafeARRAY(TypeDecorator):
    """Platform-independent ARRAY type.

    Uses PostgreSQL's ARRAY type and falls back to a comma-separated
    String for other databases.
    """
    impl = String

    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(PG_ARRAY(String))
        else:
            return dialect.type_descriptor(String)

    def process_bind_param(self, value, dialect):
        if dialect.name == 'postgresql':
            return value
        if value is not None:
            return ",".join(value)
        return None

    def process_result_value(self, value, dialect):
        if dialect.name == 'postgresql':
            return value
        if value is not None:
            return value.split(',')
        return None

    cache_ok = True


# --- Base Class ---
Base = declarative_base()


# --- Reusable Mixins ---
class SoftDeleteMixin:
    """A mixin to add soft-delete functionality to a model."""
    deleted_at = Column(DateTime(timezone=True), index=True)


class SoftDeleteWithActorMixin(SoftDeleteMixin):
    """Wizard-rebuild §11b — soft-delete + ISO 27001 actor attribution.

    Adds ``deleted_by_user_id`` on top of ``deleted_at`` so the audit
    trail records who initiated the deletion. Required for ISO 27001
    A.8.2.3 (information handling) + A.18.1.3 (protection of records)
    on wizard-managed resources (NodeDefinition, BuildSpecRecord,
    WorkflowRun, NodeVariantVerification, RepoDocsExtract).

    Kept separate from the bare ``SoftDeleteMixin`` so the existing
    soft-delete tables (Team, ApiKey, Cog, Workflow, etc.) don't
    require a backfill migration just to inherit a column that
    isn't load-bearing for their delete flows.
    """
    @declared_attr
    def deleted_by_user_id(cls):
        return Column(
            Integer,
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=False,
        )


# =============================================================================
# --- SECTION 1: ENUMS
# =============================================================================

class PermissionLevel(enum.Enum):
    """
    Permission levels for folders/cogs.
    VIEWER: Read-only access
    EDITOR: Can modify content, defaults to full editing capabilities
    OWNER: Full control over the resource
    """
    VIEWER = "viewer"
    EDITOR = "editor"
    OWNER = "owner"


class CogType(enum.Enum):
    MASTER_FOLDER = "master_folder"
    ASSET_FOLDER = "asset_folder"
    FILE = "file"
    PLACEHOLDER_FILE = "placeholder_file"
    # New types for workflow tracking (audit-protected, per-project)
    WORKFLOW_FOLDER = "workflow_folder"      # The "Workflows" folder under each project
    WORKFLOW_RUN = "workflow_run"            # Individual workflow run: 2024-01-05_AlbedoPipeline_v2.1
    WORKFLOW_STEP = "workflow_step"          # Step folder: 1_image_input, 2_albedo, etc.


class IndexingStatus(enum.Enum):
    SYNCED = "synced"
    NEEDS_UPDATE = "needs_update"
    ERROR = "error"


class TaskStatus(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PENDING_ACTION = "pending_action"
    SUCCESS = "success"
    FAILURE = "failure"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SUSPENDED = "suspended"
    AWAITING_EXTERNAL_EVENT = "awaiting_external_event"


class PermissionTargetType(enum.Enum):
    COG = "cog"
    WORKFLOW = "workflow"


class LedgerEntryType(enum.Enum):
    CREDIT_PURCHASE = "credit_purchase"
    WORKFLOW_EXECUTION = "workflow_execution"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"
    PATCH_GENERATION = "patch_generation"
    # persistent-stack-billing §1.2 — a metered reservation charge (a running
    # stack holding cpu/mem/GPU × time), ledgered distinctly from a workflow run
    # so reservation spend is auditable separately from usage spend.
    STACK_RESERVATION = "stack_reservation"
    # GH.4 — burst management fee: a per-tick debit for a burst worker's
    # (ephemeral, tenant-owned, in the customer's own cloud) elapsed
    # burst-GPU-hours × burst_pricing.burst_fee_credits_per_hour. Ledgered
    # distinctly so burst management spend is auditable separately from
    # workflow-run and reservation spend.
    BURST_MANAGEMENT_FEE = "burst_management_fee"


class InterventionStatus(enum.Enum):
    PENDING_ACTION = "pending_action"
    ACTION_TAKEN = "action_taken"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class UserStatus(enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    INACTIVE = "inactive"
    DEACTIVATED = "deactivated"


class PlatformRole(enum.Enum):
    """
    Platform-level roles for administrative access control.

    CMMC AC-5 / ISO 27001 A.9.2.3: Separation of duties

    These roles are separate from tenant-level permissions and control
    access to platform-wide administrative functions.

    Currently dormant - using is_platform_admin boolean for backwards
    compatibility. This enum enables future role-based admin access
    without schema migrations.
    """
    # Standard user - no platform admin access
    USER = "user"

    # Platform operator - can view platform metrics, manage tenant configs
    # Cannot modify billing, delete tenants, or access user data
    OPERATOR = "operator"

    # Full platform admin - all administrative capabilities
    # All actions are audit logged for accountability
    ADMIN = "admin"

    # Super admin - reserved for emergency access and system recovery
    # Requires additional verification for destructive operations
    SUPER_ADMIN = "super_admin"


class WorkflowTriggerType(enum.Enum):
    SCHEDULE = "schedule"
    WORKFLOW_COMPLETION = "workflow_completion"
    WEBHOOK = "webhook"
    FILE_EVENT = "file_event"
    LOG_PATTERN_ALERT = "log_pattern_alert"
    QUEUE_EMPTY = "queue_empty"
    # add-workflow-triggers §3 — temporal
    INTERVAL = "interval"      # {"interval_seconds": N}
    ONE_SHOT = "one_shot"      # {"run_at": "<ISO8601>"} — fires once then deactivates
    # add-workflow-triggers §4 — lifecycle
    NODE_COMPLETION = "node_completion"  # {"source_workflow_id", "workflow_node_id", "on_status"}
    FAN_IN = "fan_in"          # {"sources": [workflow_id,…], "window_seconds", "on_status"}
    # add-workflow-triggers §6 — watch
    SYSTEM_THRESHOLD = "system_threshold"  # {"metric","op","value","rearm_value","window_seconds"}
    DB_ROW_EVENT = "db_row_event"          # {"table","op":"insert|update|delete","filter":{col:val}}
    # add-workflow-triggers §5 — push (inbound)
    API_CALL = "api_call"      # authenticated, tenant-scoped fire
    EMAIL = "email"            # {"address"} — inbound email
    CHATOPS = "chatops"        # {"provider":"slack|teams","signing_secret","command"}
    GIT = "git"                # {"provider":"github|gitlab","signing_secret","events","branch"}
    # add-workflow-triggers §7 — manual / human
    MANUAL = "manual"          # authenticated manual fire (button)
    APPROVAL = "approval"      # human-in-the-loop; fires only on approve
    FORM = "form"              # form submission (public + per-trigger token)
    # add-workflow-triggers §2.3 — composite
    COMPOSITE = "composite"    # {"op":"and|or","constituents":[trigger_id,…],"window_seconds"}
    # add-workflow-triggers §8 — streaming / polling
    MESSAGE_BUS = "message_bus"  # {"provider":"redis|kafka|nats|rabbitmq|mqtt","topic","connection"}
    POLL = "poll"               # {"kind":"http|rss","url","interval_seconds"}
    # workflow-productivity-floor 4.5 — MCP tool-call receiver
    MCP_TOOL_CALL = "mcp_tool_call"


class InvitationStatus(enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"


class StorageType(str, enum.Enum):
    MINIO = "minio"
    LOCAL_DISK = "local_disk"
    S3 = "s3"  # BYO-S3 / S3-compatible endpoint (versitygw, AWS S3, ...)


class ResourceType(str, enum.Enum):
    """
    Resource types for the granular permission system.
    Each resource type has its own set of applicable capabilities.
    """
    TENANT = "tenant"                       # Tenant-wide settings and operations
    COG = "cog"                             # Folders and files
    WORKFLOW = "workflow"                   # Workflow definitions
    WORKFLOW_RUN = "workflow_run"           # Workflow executions
    WORKFLOW_NODE = "workflow_node"         # Individual workflow nodes
    NODE_DEFINITION = "node_definition"     # Node templates/definitions
    USER = "user"                           # User accounts
    TEAM = "team"                           # Team groups
    ROLE = "role"                           # Custom roles
    INVITATION = "invitation"               # User invitations
    PERMISSION_TEMPLATE = "permission_template"  # Permission templates
    API_KEY = "api_key"                     # API keys
    SECRET = "secret"                       # Stored secrets
    INTEGRATION = "integration"             # External integrations
    WORKER = "worker"                       # Processing workers
    SUBSCRIPTION = "subscription"           # Billing subscription
    AUDIT_LOG = "audit_log"                 # Audit logs
    REPORT = "report"                       # Generated reports
    NOTIFICATION = "notification"           # Notification settings
    TAG = "tag"                             # Tags/labels
    COMMENT = "comment"                     # Comments
    VERSION = "version"                     # File/workflow versions
    SHAREABLE_LINK = "shareable_link"       # Public share links
    STORAGE_LOCATION = "storage_location"   # Storage backends
    WEBHOOK = "webhook"                     # Webhook endpoints
    SCHEDULE = "schedule"                   # Scheduled jobs


# =============================================================================
# --- SECTION 2: ASSOCIATION TABLES
# =============================================================================

cog_tag_association = Table(
    "cog_tag_association",
    Base.metadata,
    Column("cog_id", ForeignKey("cogs.id"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id"), primary_key=True),
)

team_members_association = Table(
    "team_members_association",
    Base.metadata,
    Column("team_id", ForeignKey("teams.id"), primary_key=True),
    Column("user_id", ForeignKey("users.id"), primary_key=True),
)

# Role-Capability association table for granular permission system
role_capability_association = Table(
    "role_capability_association",
    Base.metadata,
    Column("role_id", ForeignKey("custom_roles.id", ondelete="CASCADE"), primary_key=True),
    Column("capability_id", ForeignKey("capabilities.id", ondelete="CASCADE"), primary_key=True),
)


# =============================================================================
# --- SECTION 3: CORE BUSINESS & USER MODELS
# =============================================================================

class Tenant(Base):
    __tablename__ = "tenants"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True)

    # Legacy credit balance (kept for backward compatibility)
    credit_balance = Column(BigInteger, default=0, nullable=False)

    # New subscription-based credit tracking
    credits_remaining = Column(BigInteger, default=0, nullable=False)  # Current period remaining credits
    credits_used_this_period = Column(BigInteger, default=0, nullable=False)  # Track usage for analytics
    credit_period_start = Column(DateTime(timezone=True), nullable=True)  # When current period started
    credit_period_end = Column(DateTime(timezone=True), nullable=True)  # When current period ends

    # OWNER-DECISIONS 09-29 — one-time credit top-ups land INSTANTLY here,
    # in a pool separate from `credits_remaining` (the plan's per-period
    # allocation). This pool NEVER expires and SURVIVES monthly renewals
    # (`utils/credit_renewal.py` / `CreditManager.reset_credit_period` must
    # never touch it). Usage draws `credits_remaining` first, then this
    # pool (`CreditManager.consume`). See migrations/20260930_tenant_purchased_credits_remaining.py.
    purchased_credits_remaining = Column(BigInteger, nullable=False, server_default='0')

    # Credit advance system (borrow from next month)
    credit_advance_used = Column(BigInteger, default=0, nullable=False)  # Amount borrowed from next period
    credit_advance_date = Column(DateTime(timezone=True), nullable=True)  # When advance was taken

    # Billing mode: 'prepaid' (subscription) or 'postpaid' (pay-as-you-go)
    billing_mode = Column(String(20), default='prepaid', nullable=False)

    # Payment method on file (for postpaid and overage billing)
    stripe_payment_method_id = Column(String(255), nullable=True)
    payment_method_last4 = Column(String(4), nullable=True)
    payment_method_brand = Column(String(50), nullable=True)
    auto_charge_enabled = Column(Boolean, default=False, nullable=False)
    billing_email = Column(String(255), nullable=True)

    # Storage tracking
    storage_used_bytes = Column(BigInteger, default=0, server_default='0')

    # Egress tracking (add-egress-billing) — bytes served OUT of this tenant's
    # MinIO bucket (tenant-{id}-files) this billing period, accumulated by the
    # egress-metering scheduler from MinIO per-bucket traffic_sent_bytes. Reset
    # to 0 on each billing-cycle reset, same as credits_used_this_period. Egress
    # beyond the plan's included_egress_gb bills as overage.
    egress_used_bytes = Column(BigInteger, default=0, nullable=False, server_default='0')

    # DERP relay bytes that transited a MOD-operated relay this billing period
    # (tailnet-plan Group 8), accumulated by the relay-metering scheduler from the
    # MOD DERP's per-account byte counters. Reset to 0 on each billing-cycle reset,
    # same as egress_used_bytes. Relay beyond the plan's included_relay_gb bills as
    # overage — a distinct line item, a token draw from the same balance as egress.
    relay_used_bytes = Column(BigInteger, default=0, nullable=False, server_default='0')

    # GG.HQ.2 — per-tenant Harbor storage quota override. NULL = derive the
    # quota from the plan (Plan.max_storage_gb, see
    # utils/harbor_quota_sync.sync_tenant_harbor_quota_from_plan); -1 =
    # unlimited (Harbor's own sentinel); >0 = exact hard cap in bytes. Set
    # via the platform-admin endpoint
    # PUT /v1/tenants/{id}/harbor-storage-quota-override, which also emits
    # an ADMIN_HARBOR_STORAGE_QUOTA_OVERRIDE audit row. The 02:00 UTC
    # reconcile (services/harbor_quota_reconcile.py) honors it, so an
    # admin-set override survives the nightly sweep instead of being
    # reverted to the plan value. See
    # migrations/20260930_tenant_harbor_quota_override.py.
    harbor_storage_quota_override_bytes = Column(BigInteger, nullable=True)

    # Per-tenant monthly overage spend cap (cents). When this period's accrued
    # overage (egress + credit) reaches the cap, further overage-incurring usage
    # is hard-stopped (usage_enforcement.check_overage_cap). NULL = inherit the
    # global default (billing_settings.default_overage_cap_cents); the admin can
    # raise it per tenant. A NULL effective cap = uncapped.
    monthly_overage_cap_cents = Column(Integer, nullable=True)

    # Add-on SKU bonuses (launch-pricing-lockdown §5). These are added
    # to the tier's base allocation each cycle reset (credits) /
    # storage cap check (storage_gb). Each unit of either purchased
    # increments the corresponding column; cancelling decrements it.
    bonus_credits = Column(BigInteger, default=0, nullable=False, server_default='0')
    # node-trial-runs — last time this tenant opened the node store; drives the
    # "N new nodes to buy" badge/notification (unowned marketplace nodes
    # published since this timestamp).
    marketplace_seen_at = Column(DateTime(timezone=True), nullable=True)
    bonus_storage_gb = Column(BigInteger, default=0, nullable=False, server_default='0')
    bonus_seats = Column(Integer, default=0, nullable=False, server_default='0')
    bonus_egress_gb = Column(BigInteger, default=0, nullable=False, server_default='0')

    # GB.PAY.1 — Stripe prepaid customer balance. Local mirror of the Stripe
    # customer credit balance (prepaid funds). Stripe is authoritative. Read
    # on the hot path only.
    prepaid_balance_cents = Column(Integer, default=0, nullable=False, server_default='0')

    # sz-payg.4 (payg-billing-hardening tasks.md §1.4) — PAYG auto-recharge
    # toggle/threshold/amount/payment-method + payg_setup SetupIntent
    # verification timestamp. SHIPS SHADOW: nothing reads or writes these
    # yet (see migrations/20261009j_payg_tenant_autorecharge_columns.py).
    auto_recharge_enabled = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    auto_recharge_threshold_cents = Column(Integer, nullable=True)
    auto_recharge_amount_cents = Column(Integer, nullable=True)
    auto_recharge_payment_method_id = Column(String(255), nullable=True)
    payg_setup_verified_at = Column(DateTime(timezone=True), nullable=True)

    # FX.89 (owner decision Q75=A) — tenant security contact. Recipient of
    # breach notices (FX.50), subprocessor change notices (FX.85) and SIEM
    # alerts (FX.77). NULL/empty = fall back to all tenant admins (see
    # services/tenant_security_contact_service.resolve_security_recipients).
    # security_contact_confirmed_at stamps the last (re)confirmation, so the
    # 6-month re-confirm reminder can select stale tenants. All nullable —
    # pre-existing tenants behave exactly as before (fallback path).
    security_contact_email = Column(String(255), nullable=True)
    security_contact_phone = Column(String(64), nullable=True)
    security_contact_secondary_email = Column(String(255), nullable=True)
    security_contact_confirmed_at = Column(DateTime(timezone=True), nullable=True)

    # Concurrent process tracking
    concurrent_processes_running = Column(BigInteger, default=0, nullable=False)

    # Retention & Compliance settings
    default_retention_days = Column(Integer, default=2555, nullable=False)  # ~7 years default
    allow_hard_delete = Column(Boolean, default=False, nullable=False)  # If False, never hard delete
    legal_hold_enabled = Column(Boolean, default=True, nullable=False)  # Allow setting legal holds

    # workflow-agentic-execution-loop §workflow-node-output-cache —
    # per-tenant gate for the node-output cache read-short-circuit.
    # Default False at first deploy; writes happen always-on regardless
    # so flipping the flag on later picks up immediate benefit.
    output_cache_enabled = Column(
        Boolean, default=False, nullable=False, server_default='false',
    )
    # workflow-agentic-execution-loop §port-type-enforcement —
    # per-tenant gate for auto-bridging incompatible wizard-proposed
    # edges with adapter nodes. Default False; even off, the wizard
    # still hard-rejects type mismatches at the validator (existing
    # behaviour).
    port_type_enforcement_enabled = Column(
        Boolean, default=False, nullable=False, server_default='false',
    )
    # workflow-agentic-execution-loop §control-flow-primitives —
    # per-tenant gate for executing conditional / fan_out_over_list /
    # retry_with_params node kinds. Default False; the orchestrator
    # rejects workflows containing these primitives when off.
    control_flow_primitives_enabled = Column(
        Boolean, default=False, nullable=False, server_default='false',
    )
    # workflow-agentic-execution-loop §wizard-agentic-loop —
    # per-tenant gate for the agent driver (compose → test-input →
    # dispatch → critic → iterate). Default False; legacy
    # Builder+Critic wizard pipeline runs when off.
    wizard_agentic_loop_enabled = Column(
        Boolean, default=False, nullable=False, server_default='false',
    )

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Encryption
    encryption_key = Column(String, nullable=True)  # Will be encrypted with the master key

    # Relationships
    subscription = relationship("Subscription", back_populates="tenant", cascade="all, delete-orphan", uselist=False)
    settings = relationship("TenantSetting", back_populates="tenant", cascade="all, delete-orphan")
    # Soft-delete for ISO 27001 compliance. Hard-deleting a tenant would
    # cascade-destroy every workflow run, credit ledger entry, and audit
    # event attributed to it — breaking the audit chain. Instead, stamp
    # deleted_at + scrub the display name; all tenant users are also
    # soft-deleted by the delete endpoint. Listings filter `deleted_at IS NULL`.
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Hard-delete date (ISO AU-11 + SC-28). Set by the archive endpoint to
    # deleted_at + retention_days; the retire_archived_tenants scheduler
    # job purges Harbor projects, MinIO buckets, Vault secrets, and the
    # DB row after this date. NULL means "keep forever" (legal hold or
    # compliance lock).
    hard_delete_after = Column(DateTime(timezone=True), nullable=True, index=True)
    # Per-tenant override of default_retention_days — defaults to 90 for
    # SaaS tenants created via onboarding, longer for contracted
    # enterprise tenants (set via admin API).
    retention_days = Column(Integer, default=90, nullable=False)
    # GSEC R-015 / FX.15 — stamped by the privacy-erasure sweep
    # (services/scheduler.py::_erase_tenant_bytes) when this tenant's
    # object-store bytes were destroyed and its Fernet encryption key
    # shredded. NULL = bytes still on the object store. The row itself is
    # NEVER deleted (ISO 27001 — no hard deletes); this column only makes
    # the byte/key destruction idempotent and auditable. BYO-S3 tenants are
    # report-only (customer buckets are never touched) but are still
    # stamped, with the flag recorded in the TENANT_BYTES_PURGED audit row.
    bytes_purged_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Set to True by check_harbor_quotas when used/hard >= 0.95. While
    # true, /build-and-register-local refuses new builds with an upgrade
    # prompt; pulls still work. Cleared automatically when usage drops
    # below the threshold (e.g. after the operator deletes old images).
    quota_push_blocked = Column(Boolean, default=False, nullable=False)

    # launch-completion-megaplan GC.PFM — shared calibration-fixture state.
    # fixture_status: 'pending' (default) -> 'ready' | 'failed' via the
    # fixture-migrator callback; the daily drift tick flips 'ready' back
    # to 'drift' (alert-only — PFM.1: no auto-repair).
    fixture_status = Column(String(16), nullable=True, default='pending', server_default='pending')
    # Manifest version of the last successful shared-bucket upload
    # (platform-calibration-data).
    fixture_version = Column(String(32), nullable=True)
    # When the migrator/audit last verified this tenant's fixtures.
    fixture_checked_at = Column(DateTime(timezone=True), nullable=True)

    # GSEC FX.59 part 2 (owner decision Q42=A) — contract-end lifecycle
    # state machine: 'active' (normal), 'read_only' (days 0-30 after
    # contract end — writes blocked, reads/exports open), 'suspended'
    # (day 30-60 — everything blocked; reactivation within the grace
    # period restores everything). Set via services (part 4 admin
    # endpoints) through utils.tenant_lifecycle.set_lifecycle_state,
    # which stamps lifecycle_changed_at + lifecycle_reason and writes
    # the TENANT_LIFECYCLE_STATE_CHANGED audit row. The enforcement
    # dependency (require_tenant_writable / require_tenant_active in
    # utils/tenant_lifecycle.py) raises 423/403 from the column on each
    # request; platform admins bypass.
    lifecycle_state = Column(
        String(20), nullable=False,
        default="active", server_default="active",
    )
    lifecycle_changed_at = Column(DateTime(timezone=True), nullable=True)
    lifecycle_reason = Column(String(500), nullable=True)

    users = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    teams = relationship("Team", back_populates="tenant")
    cogs = relationship("Cog", back_populates="tenant")
    email_config = relationship("TenantEmailConfig", back_populates="tenant", uselist=False,
                                cascade="all, delete-orphan")
    permission_settings = relationship("TenantPermissionSettings", back_populates="tenant", uselist=False, cascade="all, delete-orphan")
    permission_templates = relationship("PermissionTemplate", back_populates="tenant")

    __table_args__ = (
        CheckConstraint(
            "retention_days BETWEEN 30 AND 2555",
            name="ck_tenants_retention_days_range",
        ),
        # GSEC FX.59 part 2 — lifecycle state machine values.
        CheckConstraint(
            "lifecycle_state IN ('active', 'read_only', 'suspended')",
            name="ck_tenants_lifecycle_state",
        ),
    )

    @property
    def current_plan_id(self):
        """Plan id of the tenant's active subscription, or None if it has
        no subscription. Lets the admin custom-plans table tell whether a
        plan is actually assigned instead of guessing."""
        return self.subscription.plan_id if self.subscription else None


class User(Base):
    """
    Represents a user in the system. Each user is associated with a single tenant
    and can have a role within that tenant.
    """
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, index=True)
    keycloak_user_id = Column(String(255), unique=True, index=True, nullable=True)

    # User profile information
    username = Column(String(100), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    first_name = Column(String(100))
    last_name = Column(String(100))
    onboarding_complete = Column(Boolean, default=False, server_default='f')
    # activation-lifecycle-emails: per-user opt-out for activation/marketing
    # lifecycle mail. Does NOT suppress transactional mail (verify, billing,
    # security). Default false = opted in.
    lifecycle_emails_opted_out = Column(Boolean, default=False, server_default='f', nullable=False)

    # Status and role information
    status = Column(Enum(UserStatus, name="user_status_enum"), default=UserStatus.PENDING)
    custom_role_id = Column(Integer, ForeignKey('custom_roles.id'))
    custom_role = relationship('CustomRole', back_populates='users')

    # External user tracking
    is_external = Column(Boolean, default=False, server_default='f', nullable=False)
    invited_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=True)

    # FX.110 (owner decision Q97=A) — outside-collaborator lifecycle columns.
    # External accounts carry a hard end date (default
    # external_collab.default_expiry_days, tenant-scoped platform setting);
    # the scheduler tick (services/scheduler.py FX.110 block) soft-disables
    # expired access — never a hard delete.
    # End date for this external account's access (stamped from the
    # invitation expiry on accept). NULL = not an external account /
    # no expiry set.
    external_access_expires_at = Column(DateTime(timezone=True),
                                        nullable=True, index=True)
    # Dedupe stamp for the "expires in N days" reminder to the inviter
    # (external_collab.reminder_days_before, default 7). Set once, so the
    # daily job never re-reminds the same expiry.
    external_expiry_reminded_at = Column(DateTime(timezone=True), nullable=True)
    # Terms gate: when the tenant has set external_collab.terms_text, the
    # external user must accept before first API access; this stamps the
    # acceptance moment (the audit row carries the terms version hash).
    # NULL = not yet accepted.
    external_terms_accepted_at = Column(DateTime(timezone=True), nullable=True)
    # Gov/DoD profiles (cmmc / fedramp_high_il5) force
    # external_collab.require_admin_approval: each external user needs an
    # explicit tenant-admin approval before their first access. Stamps the
    # approving admin (users.id); NULL = not approved.
    external_profile_approved_by_user_id = Column(
        Integer, ForeignKey('users.id'), nullable=True, index=True)

    # Platform administration
    # CMMC AC-5 / ISO 27001 A.9.2.3: Separation of duties
    is_platform_admin = Column(Boolean, default=False, server_default='f', nullable=False)
    # Future-proofing: Role-based platform access (dormant until migration)
    # When activated, platform_role will supersede is_platform_admin
    platform_role = Column(
        Enum(PlatformRole, name="platform_role_enum", create_constraint=True),
        default=PlatformRole.USER,
        nullable=False
    )

    # Soft-delete for ISO 27001 / GDPR-style compliance. When set, the user
    # row is kept (so every FK referring to this user stays intact for audit
    # attribution) but PII fields (email, first_name, last_name, username)
    # are scrubbed. Listing endpoints filter `deleted_at IS NULL`. Login is
    # blocked by virtue of the Keycloak account being deleted at the same
    # time. Never hard-delete a user — it breaks audit chains.
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # GSEC R-016 / FX.15 — stamped the moment the user requests account
    # deletion (services/me_identity_service.py::request_account_deletion)
    # or is soft-deleted (crud/user_cud.py::delete_user_permanently). The
    # privacy-erasure sweep uses the oldest of (deletion_requested_at,
    # deleted_at) as its grace anchor (privacy.erasure_grace_days,
    # default 30).
    deletion_requested_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # GSEC R-016 / FX.15 — stamped when the privacy-erasure sweep finished
    # this user's byte purge (objects their File rows reference) and PII
    # scrub. NULL = erasure pending. The row itself stays (ISO 27001 —
    # no hard deletes).
    erased_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # GY.V5 — the ``minor`` data label: this user's data is a minor's
    # (COPPA / under-13). A stored flag read by the gate, mirroring how
    # GY.C7 labels are represented (``cogs.data_classification``). Effects:
    #  (a) external-node/LLM processing of this subject's data is blocked
    #      via the C7 label gate (utils/label_policy.py);
    #  (b) FX.15 privacy erasure is ACCELERATED for flagged subjects — the
    #      grace window (privacy.erasure_grace_days) is skipped;
    #  (c) the DSAR export includes the flag + the subject's consent
    #      records (services/me_identity_service.py::export_personal_data).
    # Set by a platform admin; never hard-deleted.
    is_minor = Column(Boolean, default=False, server_default='f',
                      nullable=False, index=True)

    # Partial-signup tracking. Stamps the moment the signup welcome
    # email made it out the door (set in routers/onboarding.py's
    # send-success branch); welcome_email_error captures the SMTP /
    # template error message when the send failed so /platform/
    # signup-orphans can show ops WHY a user never got their welcome.
    # NULL on every account created before this feature shipped or on
    # accounts created via paths that don't send a welcome (platform
    # admin, on-prem self-host).
    welcome_email_sent_at = Column(DateTime(timezone=True), nullable=True)
    welcome_email_error = Column(Text, nullable=True)

    # Tenancy and hierarchy
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='users')
    parent_user_id = Column(Integer, ForeignKey('users.id'))
    children = relationship('User', back_populates='parent_user', remote_side=[id], foreign_keys='[User.parent_user_id]')
    parent_user = relationship('User', back_populates='children', remote_side=[parent_user_id], foreign_keys=[parent_user_id])

    # External user invitation relationship
    invited_by = relationship('User', foreign_keys=[invited_by_user_id], remote_side=[id])

    # Resource ownership and associations
    owned_cogs = relationship('Cog', back_populates='owner')
    api_keys = relationship('ApiKey', back_populates='user')
    # Gate 30: the legacy Permission model is deleted; grants live in ResourcePermission.
    notifications = relationship("Notification", back_populates="recipient")
    settings = relationship("UserSetting", back_populates="user")

    teams = relationship(
        'Team',
        secondary=team_members_association,
        back_populates='members'
    )

    # Usage and audit fields
    storage_attributed_bytes = Column(BigInteger, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # add-self-service-sso Phase E — JIT provisioning columns.
    # provisioning_source distinguishes JIT-created users from manual /
    # invited signups; drives the §7.7 skip-MOD-side-verify-email
    # behaviour. is_email_verified mirrors Keycloak's emailVerified for
    # MOD-side gates (e.g. invoice email sends). email_verified_at is
    # the backbone for the deferred §6.5 90-day staleness check.
    # last_sso_idp_id lights up §3.5's revoke-on-IdP-delete sweep.
    provisioning_source = Column(String(16), nullable=False,
                                 server_default='manual', default='manual')
    is_email_verified = Column(Boolean, nullable=False,
                               server_default='f', default=False)
    email_verified_at = Column(DateTime(timezone=True), nullable=True)
    # FK enforced at DB-level by the migration; the ORM-side mapping
    # stays a plain Integer because tenant_idp_configs has no ORM class
    # yet (Phase 2 SSO tables are raw-SQL only), and a metadata-time
    # FK reference would fail Base.metadata.create_all().
    last_sso_idp_id = Column(Integer, nullable=True)

    # add-scim-directory-sync §2.2 — SCIM IdP's stable identifier.
    # Persists across user-side email changes; we upsert by this on
    # SCIM PUT / PATCH when present. Tenant-scoped uniqueness enforced
    # by the partial index uix_users_tenant_external_id.
    external_id = Column(String(255), nullable=True)
    smtp_settings = relationship("SMTPSettings", back_populates="owner", uselist=False, cascade="all, delete-orphan")

    # launch-day-followup-5 §1A — Postmark bounce flag.
    # Set by the bounce webhook when a user has ≥3 bounces in 30 days.
    # Surfaces in the platform-admin Users tab; admin clears after
    # investigating (re-add to allowlist or re-verify the address).
    email_bounce_flag = Column(Boolean, default=False, server_default='f', nullable=False)

    # Internal service-account flag. True for the seeded api_service user
    # used by build-task containers / orchestrator-internal HTTP calls.
    # Service accounts get special-cased in routers that need to attribute
    # writes to "the platform" rather than a real human: e.g. tenant
    # `NodeDefinition` rows created by the wizard build pipeline land as
    # tenant-shared (user_id IS NULL) instead of user-private to the
    # service account, and auto-flip is_in_marketplace=true. Replaces the
    # previous `username == "api_service"` literal check which silently
    # failed in production where the seeded user is `api@service.local`.
    is_service_account = Column(Boolean, default=False, server_default='f', nullable=False)


class StorageConnection(SoftDeleteMixin, Base):
    """Per-tenant BYO-S3 storage connection (remove-minio-versitygw Gate 5, D6).

    Replaces the dead, plaintext, user-scoped ``Drive`` stub. Points MOD at an
    S3-compatible endpoint the tenant controls. The secret key is NEVER stored
    on this row — it is persisted via the established tenant-secret pattern
    (Fernet + per-tenant key + Vault dual-write, ``crud/secret_cud.py``) under
    the Secret name held in ``secret_ref``. Only a non-secret reference lives
    here.

    Activation is gated on a passing connection test (``last_test_ok``). At most
    one active connection per tenant; when none is active the tenant falls back
    to the house-default backend + ``tenant-{id}-files``.
    """
    __tablename__ = "storage_connections"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    name = Column(String(255), index=True, nullable=False)
    storage_type = Column(Enum(StorageType, native_enum=False, length=32), nullable=False,
                          default=StorageType.S3, server_default=StorageType.S3.value)
    endpoint = Column(String, nullable=False)          # host[:port] or URL of the S3 endpoint
    region = Column(String, nullable=False, default="us-east-1", server_default="us-east-1")
    bucket = Column(String, nullable=False)
    access_key = Column(String, nullable=False)        # NON-secret (paired with the Vault-held secret)
    secret_ref = Column(String, nullable=True)         # Secret.name holding the encrypted secret key
    secure = Column(Boolean, default=True, server_default='t', nullable=False)  # TLS to the endpoint
    # GSEC.Q21.1 — opt-in to scoped, short-lived STS AssumeRole session creds
    # for this BYO-S3 connection (task containers get tenant-bucket + run-prefix
    # scoped keys instead of the tenant's static key). Default False = today's
    # static-key behaviour; the worker-credential mint still falls back to the
    # static key (with a warning) if the endpoint does not actually support STS.
    sts_enabled = Column(Boolean, default=False, server_default='f', nullable=False)
    is_active = Column(Boolean, default=False, server_default='f', nullable=False)
    last_test_ok = Column(Boolean, default=False, server_default='f', nullable=False)
    last_test_at = Column(DateTime(timezone=True), nullable=True)
    last_test_error = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # WS4.Q17.1 — migrate-on-activate: when a tenant swaps their active BYO-S3
    # connection, existing objects under the OLD backend get copied to the NEW
    # one before cutover. These fields track that background migration; they
    # are read-only from the API's perspective (set by the migration worker),
    # never accepted on StorageConnectionCreate/Update.
    #   none     — no migration ever requested (default; also the terminal
    #              state for a tenant's very first connection, nothing to move)
    #   pending  — migration requested, not yet picked up
    #   running  — migration in progress
    #   verified — migration finished and object count/checksum verified
    #   failed   — migration attempted and failed; see migration_error
    migration_status = Column(String(16), nullable=False, default='none', server_default='none')
    migration_started_at = Column(DateTime(timezone=True), nullable=True)
    migration_completed_at = Column(DateTime(timezone=True), nullable=True)
    migrated_object_count = Column(BigInteger, nullable=False, default=0, server_default='0')
    migrated_bytes = Column(BigInteger, nullable=False, default=0, server_default='0')
    migration_error = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_storage_connection_tenant_name"),
        CheckConstraint(
            "migration_status IN ('none', 'pending', 'running', 'verified', 'failed')",
            name="ck_storage_connection_migration_status",
        ),
    )


class Team(SoftDeleteMixin, Base):
    __tablename__ = "teams"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), index=True)
    name = Column(String(255))
    # Relationships
    tenant = relationship("Tenant", back_populates="teams")
    members = relationship("User", secondary=team_members_association, back_populates="teams")
    # Gate 30: the legacy Permission model is deleted; grants live in ResourcePermission.
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tenant_team_name"),)


# =============================================================================
# --- SECTION 4: API KEY & SECURITY MODELS
# =============================================================================

# 30.6.3 (c3-J1) — the scopes a per-stack DEPLOY token may carry (kept next to
# ApiKey until the stack_deploy_token service lands, c3-J3).
API_KEY_DEPLOY_TOKEN_SCOPES = frozenset(
    {"deploy", "logs", "backups:read", "backups:run", "seed"})

# 30.6.3 size-token-merge (c3-J1 alias only; no column change) — the stack-key
# scope vocabulary under the general API-key machinery.
API_KEY_STACK_SCOPES = API_KEY_DEPLOY_TOKEN_SCOPES


class ApiKey(SoftDeleteMixin, Base):
    """
    API Key model with Keycloak service account integration.

    CMMC/ISO 27001 Compliance:
    - API keys are mapped to Keycloak service accounts
    - Tokens are short-lived OAuth 2.0 JWTs (not static keys)
    - Full audit trail via Keycloak events
    - Automatic expiration enforcement
    """
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id"), index=True)
    key_prefix = Column(String(8), unique=True)
    hashed_key = Column(String(255))
    description = Column(String(255))
    expires_at = Column(DateTime(timezone=True))
    last_used_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)

    # Keycloak service account mapping (for OAuth token exchange)
    # These fields enable the API key → OAuth token flow
    keycloak_client_id = Column(String(255), unique=True, nullable=True)
    keycloak_client_uuid = Column(String(36), nullable=True)  # Keycloak internal UUID
    # Note: keycloak_client_secret is stored encrypted, not plaintext

    # Unified-authorization Gate 34: optional key scope. The defaults mean
    # UNSCOPED — a key with is_read_only=False and capability_scope=NULL behaves
    # exactly as before (full owner permissions). authorize() caps a scoped key's
    # effective capabilities (Gate 35): a read-only key is refused every mutating
    # capability (Gate 36); capability_scope, when set, is an allowlist of
    # "RESOURCE_TYPE:capability" strings the key is permitted to use.
    is_read_only = Column(Boolean, nullable=False, default=False, server_default="false")
    capability_scope = Column(JSONB, nullable=True)
    encrypted_keycloak_secret = Column(Text, nullable=True)

    # GX.A3 — app binding: a key with app_id set is an APP KEY, scoped to
    # the App row's allowed_workflow_ids / allowed_folder_ids (the scope
    # lives on the app, not the key — rotating the key keeps the scope).
    # NULL = a normal key, scope check is a no-op.
    app_id = Column(ForeignKey("apps.id"), nullable=True, index=True)

    # FX.114 (owner Q101=A) — service-account ownership + lifecycle.
    # A key with service_account_id set is owned by the tenant's
    # non-human principal (its named human owner still governs review);
    # NULL = user-owned (today's behaviour).
    service_account_id = Column(
        ForeignKey("service_accounts.id"), nullable=True, index=True)
    # FX.114 — soft-disabled by the auto-disable job (expired / unused
    # past the tenant max-unset window). Revocation remains deleted_at
    # (soft delete); disabled keys are rejected exactly like revoked
    # ones at the auth funnel, audited as disabled.
    disabled_at = Column(DateTime(timezone=True), nullable=True)
    # FX.114 — rotation: rotates_to_key_id points at the successor key;
    # rotation_disable_at is the moment the OLD key's overlap window
    # ends (the job auto-disables it then).
    rotates_to_key_id = Column(Integer, nullable=True)
    rotation_disable_at = Column(DateTime(timezone=True), nullable=True)
    # GSEC FX.152 part 2 (owner decision Q139=B) — tenant-admin rotation with
    # a grace window. rotated_at stamps when this key was rotated (its
    # successor lives in rotates_to_key_id); grace_until is the moment the
    # grace window ends and the key is soft-disabled (services/api_key_grace.py
    # enforces it at the auth funnel, writing API_KEY_GRACE_EXPIRED once).
    # Nullable so the bind-mounted model is live for new processes before the
    # ALTER TABLE runs (migrations/20261007h_fx152_rotate_audit.py).
    rotated_at = Column(DateTime(timezone=True), nullable=True)
    grace_until = Column(DateTime(timezone=True), nullable=True)
    # FX.114 — expiry-reminder dedupe: one 14-day and one 3-day
    # reminder per key, ever (flag set only after a successful send).
    reminder_14d_sent = Column(Boolean, nullable=False, default=False,
                               server_default="false")
    reminder_3d_sent = Column(Boolean, nullable=False, default=False,
                              server_default="false")

    # FX.78.p6 — why a key was soft-deleted (revoked). SCIM deprovisioning
    # stamps "scim_deprovision" so re-provisioning (PATCH/PUT active=true)
    # can restore EXACTLY the keys it revoked and no others (a key the user
    # revoked themselves stays revoked — ISO 27001 A.9.2.6). NULL = revoked
    # by any other path. Nullable: migrations/20261007zp_fx78_p6_api_key_revocation_reason.py
    # adds the ALTER TABLE (dry-run until deployed).
    deleted_at_reason = Column(String(64), nullable=True)

    # 30.6.3 (c3-J1) — per-stack DEPLOY credential pin. A row with this set IS
    # a deploy token: it may only act on that one stack (own-stack pin, c3-J2
    # allowlist), and its scopes live in capability_scope as
    # "stack:<scope>" (API_KEY_DEPLOY_TOKEN_SCOPES). NULL = an ordinary key,
    # today's behaviour unchanged. migrations/20261027a_api_keys_deploy_token_stack_id.py
    deploy_token_stack_id = Column(
        ForeignKey("persistent_stacks.id"), nullable=True, index=True)

    # Relationships
    user = relationship("User", back_populates="api_keys")
    app = relationship("App", foreign_keys=[app_id])
    service_account = relationship("ServiceAccount")


class App(SoftDeleteMixin, Base):
    """GX.A3 — an app built on MOD.

    One App == one Keycloak client == one API key (api_key_id) == one
    proxy route. The key is the app's identity; the SCOPE is the app's:
    allowed_workflow_ids / allowed_folder_ids (JSONB lists) bound what the
    app's key may touch (services/app_key_scope.py enforces it, default
    deny). ISO 27001: soft-delete only (deleted_at), never hard-deleted.
    """
    __tablename__ = "apps"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    slug = Column(String(63))            # subdomain label
    hostname = Column(String(253))       # slug.BASE_DOMAIN, unique while live
    name = Column(String(255))
    # provisioning | active | provisioning_failed | revoked
    status = Column(String(32), nullable=False, default="provisioning", server_default="provisioning")
    failure_detail = Column(JSONB, nullable=True)   # which provisioning step failed
    target_kind = Column(String(16))   # stack-service | external-url
    stack_id = Column(ForeignKey("persistent_stacks.id"), nullable=True)
    stack_service = Column(String(127), nullable=True)
    stack_port = Column(Integer, nullable=True)
    external_url = Column(String(1024), nullable=True)
    kc_client_id = Column(String(255), unique=True, nullable=True)
    kc_client_uuid = Column(String(36), nullable=True)
    api_key_id = Column(ForeignKey("api_keys.id"), nullable=True)
    allowed_workflow_ids = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    allowed_folder_ids = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    # GX.S1 — per-app forward-auth config: {"require_login": bool,
    # "allowed_groups": [str], "public_paths": [str]}. NULL = defaults
    # (public app, no forward_auth rendered). Migration 20261002_app_forward_auth.
    forward_auth_config = Column(JSONB, nullable=True)
    created_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_apps_tenant_slug"),
        CheckConstraint(
            "status IN ('provisioning', 'active', 'provisioning_failed', 'revoked')",
            name="ck_apps_status",
        ),
    )


class ServiceAccount(SoftDeleteMixin, Base):
    """FX.114 (owner Q101=A) — a tenant-owned, non-human principal.

    A service account is NOT a user: it has no Keycloak identity and
    cannot sign in. It OWNS API keys (api_keys.service_account_id) so
    the credentials outlive any single engineer, and it always carries
    a REQUIRED named human owner (owner_user_id) — the accountable
    person who answers for the credentials in access reviews. When the
    owner is disabled/offboarded the row is flagged
    ``owner_reassignment_required`` (never orphaned silently).

    ISO 27001: soft-delete only (deleted_at) + ``disabled_at`` for the
    explicit disable state; never hard-deleted.
    """
    __tablename__ = "service_accounts"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(String(255), nullable=True)
    # REQUIRED named human owner — the accountable person for this
    # principal's credentials (access reviews always resolve to a user).
    owner_user_id = Column(ForeignKey("users.id"), nullable=False,
                           index=True)
    # Set by the user-disable hook while owner_user_id points at a
    # non-active user; cleared the moment the owner is reassigned to an
    # active user.
    owner_reassignment_required = Column(Boolean, nullable=False,
                                         default=False,
                                         server_default="false")
    # FX.114 — explicit disable state (distinct from soft-delete):
    # disabled service accounts cannot hold or mint LIVE keys.
    disabled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)

    tenant = relationship("Tenant")
    owner = relationship("User", foreign_keys=[owner_user_id])


class AppUserType(SoftDeleteMixin, Base):
    """GX.S1b — a user TYPE an app declares (e.g. `consignor`).

    App users authenticate through the platform IdP via the APP's KC client
    (GX.A2) and live in KC group ``app:<slug>:<type>`` in the SAME realm —
    but they are ADDITIVE and outside MOD: they must NEVER get a `users` row
    (that invariant is what keeps them out of every MOD route; see the
    APP_USER_NO_PLATFORM_ACCESS refusal in dependencies.get_current_user).
    Forward-auth passes the type as ``X-Mod-User-Type`` (GX.S1); the app
    decides what such a user sees.

    ``self_signup`` gates whether an unknown KC sub that authenticates to this
    app with this type may be auto-registered on first login (otherwise
    invites only). ISO 27001: soft-delete only. Migration
    20261002e_app_user_types.
    """
    __tablename__ = "app_user_types"
    id = Column(Integer, primary_key=True)
    app_id = Column(ForeignKey("apps.id"), nullable=False, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String(63), nullable=False)          # machine key, e.g. "consignor"
    label = Column(String(255), nullable=True)         # human display name
    self_signup = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("app_id", "name", name="uq_app_user_types_app_name"),
        CheckConstraint("name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'", name="ck_app_user_types_name"),
    )


class AppUser(SoftDeleteMixin, Base):
    """GX.S1b — one identity an app admitted under one of its user types.

    Maps a KC ``sub`` (per app) to a user type. The KC user is created/kept
    by the app-management flow (services/app_user_types.py) and assigned to
    KC group ``app:<slug>:<type>`` in the shared realm; disabling here is
    SOFT (status='disabled' + disabled_at, KC group removal) — the KC user is
    NEVER deleted (they may belong to other apps). No `users` row is ever
    created for an app user (see the class docstring above). Migration
    20261002e_app_user_types.
    """
    __tablename__ = "app_users"
    id = Column(Integer, primary_key=True)
    app_id = Column(ForeignKey("apps.id"), nullable=False, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    app_user_type_id = Column(ForeignKey("app_user_types.id"), nullable=False, index=True)
    kc_sub = Column(String(255), nullable=False, index=True)
    email = Column(String(320), nullable=False)
    user_type = Column(String(63), nullable=False)     # denormalised type name
    status = Column(String(16), nullable=False, default="active", server_default=text("'active'"))
    invited_by = Column(ForeignKey("users.id"), nullable=True)  # NULL = self-signup
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    disabled_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("app_id", "kc_sub", name="uq_app_users_app_sub"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_app_users_status"),
    )


class TenantBurstSettings(Base):
    """burst-to-saas 3.2 — the brakes, one row per tenant.

    `enabled` is a KILL SWITCH, not an enable switch (design D2). Turning it
    off disables burst everywhere immediately; turning it on enables nothing
    by itself, because eligibility still lives per-workflow. Asymmetric on
    purpose: a symmetric switch lets an admin flip one control and silently
    make every workflow in the tenant eligible, which is exactly the
    irreversible mistake this change is shaped to prevent.

    `spill_threshold_minutes` is a WAIT, not a queue depth (design D3). Queue
    depth is not something a person can reason about — its meaning shifts with
    task size, worker count and concurrency — while "how long before this
    starts" is the thing they actually care about.

    `spend_ceiling` bounds cost before it is spent. Reaching it makes runs
    stay LOCAL rather than fail (D4): a cost control that turns into a
    work-loss control gets switched off by the first person it bites.
    """
    __tablename__ = "tenant_burst_settings"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       unique=True, index=True, nullable=False)

    enabled = Column(Boolean, default=False, server_default='f', nullable=False)
    spill_threshold_minutes = Column(Integer, default=30, server_default='30', nullable=False)
    spend_ceiling = Column(Numeric(12, 2), nullable=True)
    spend_period_days = Column(Integer, default=30, server_default='30', nullable=False)

    updated_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    tenant = relationship("Tenant")


class Secret(SoftDeleteMixin, Base):
    __tablename__ = "secrets"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    created_by_user_id = Column(ForeignKey("users.id"))
    name = Column(String(255), index=True)
    encrypted_value = Column(Text, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    shared_with_all_tenants = Column(Boolean, default=False, nullable=False)
    shared_with_tenant_ids = Column(SafeARRAY, nullable=True)

    # FX.117 PART 2 (owner Q104=A) — per-secret workflow scope.
    # NULL = current behaviour (any workflow of the tenant). When set,
    # the by-name run-token injection path only serves the secret to
    # runs of these workflows (403 + audited SECRET_ACCESS_DENIED
    # otherwise).
    allowed_workflow_ids = Column(JSONB, nullable=True)
    # FX.117 PART 2 — optional expiry. expires_at in the past refuses
    # run-time injection (403 + audited); expired secrets are NEVER
    # hard-deleted (ISO: soft state only) — the daily sweep stamps
    # expired_notified_at and reminds the owner ahead of time at the
    # platform-setting reminder days (secrets.expiry_reminder_days).
    expires_at = Column(DateTime(timezone=True), nullable=True)
    expiry_reminders_sent = Column(JSONB, nullable=True)  # [day, ...] dedupe
    expired_notified_at = Column(DateTime(timezone=True), nullable=True)

    # fortress-6.5 S1b (migration 20261013c) — HE-key custody model on the
    # existing secrets table. kind NOT NULL default 'generic' so existing
    # rows read 'generic' via the server_default (additive, no backfill).
    kind = Column(String(32), nullable=False, server_default=text("'generic'"))
    custody = Column(String(16), nullable=True)
    he_key_fingerprint = Column(String(128), nullable=True)

    creator = relationship("User")
    __table_args__ = (
        CheckConstraint("kind IN ('generic', 'he_key')",
                        name="ck_secrets_kind"),
        CheckConstraint("custody IS NULL OR custody IN "
                        "('custodial', 'non_custodial')",
                        name="ck_secrets_custody"),
        CheckConstraint("kind <> 'he_key' OR custody IS NOT NULL",
                        name="ck_secrets_he_key_custody"),
        Index("ix_secrets_he_key_fingerprint", "he_key_fingerprint"),
    )


class SecretWorkflowScope(SoftDeleteMixin, Base):
    """FX.117 p3b — one ALLOWED (secret, workflow) pair = a scope row.

    FX.117 p3a shipped the value-masking half; p3b gives a tenant admin the
    power to restrict a tenant secret so ONLY the listed workflows may resolve
    it at run-time injection. NO rows for a secret = today's behaviour (every
    workflow of that tenant).

    NEW TABLE ONLY (models.py is bind-mounted live: no column was added to
    any existing table — the p2 `allowed_workflow_ids` JSONB bag stays as the
    admin-facing view and this table is its authoritative backing store).
    SOFT-DELETE ONLY (ISO 27001: replacing a scope soft-deletes the removed
    rows and stamps `deleted_at`; nothing is ever hard-deleted), and the
    secret_id FK carries NO `ON DELETE CASCADE` clause — deleting a secret
    must not drop the compliance record of what scope it once had.

    The DB enforces the pair uniqueness over LIVE rows only
    (`deleted_at IS NULL`), so a replace that re-adds a previously removed
    pair never collides with its own tombstone.
    """
    __tablename__ = "secret_workflow_scopes"
    # BigInteger/Integer variant (same trick as AuditEvent) so the test
    # suite's engine autoincrements the key while Postgres keeps BIGSERIAL.
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # Tenant that owns the scope (mirrors the secret's tenant; kept explicit
    # so the tenant-admin gate can scope a query without joining secrets).
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    secret_id = Column(ForeignKey("secrets.id"), nullable=False, index=True)
    workflow_id = Column(ForeignKey("workflows.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    created_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    deleted_by_user_id = Column(ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        Index("uq_secret_workflow_scopes_live", "secret_id", "workflow_id",
              unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("ix_secret_workflow_scopes_secret", "secret_id"),
        Index("ix_secret_workflow_scopes_tenant", "tenant_id"),
    )


class SecretExpiry(SoftDeleteMixin, Base):
    """FX.117 p4a — the per-secret expiry RECORD (NEW table, one live row
    per secret).

    models.py is bind-mounted LIVE and shared by every lane, so NO column was
    added to the existing `secrets` table: the expiry date a tenant admin sets
    lives here, and the p2 `Secret.expires_at` bag stays as the admin-facing
    view this table mirrors (same shape as p3b's `SecretWorkflowScope`
    mirroring `Secret.allowed_workflow_ids`).

    SOFT-DELETE ONLY (ISO 27001): clearing an expiry soft-deletes the row and
    stamps `deleted_at`; nothing is ever hard-deleted. The secret_id FK
    carries NO `ON DELETE CASCADE` clause — deleting a secret must not drop
    the compliance record of the expiry it once had.

    The DB enforces at most ONE LIVE row per secret (`deleted_at IS NULL` in
    the partial unique index), so a replace that moves an expiry forward never
    collides with its own tombstone.
    """
    __tablename__ = "secret_expiries"
    # BigInteger/Integer variant (same trick as AuditEvent) so the test
    # suite's engine autoincrements the key while Postgres keeps BIGSERIAL.
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # Tenant that owns the expiry (mirrors the secret's tenant; explicit so
    # the tenant-admin gate can query without joining secrets).
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    secret_id = Column(ForeignKey("secrets.id"), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    # The exactly-once-per-interval record for the p4b reminder job: the
    # last reminder actually delivered (NULL = never reminded).
    last_reminded_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    created_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    deleted_by_user_id = Column(ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        Index("uq_secret_expiries_live", "secret_id",
              unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("ix_secret_expiries_secret", "secret_id"),
        Index("ix_secret_expiries_tenant", "tenant_id"),
        Index("ix_secret_expiries_expires_at", "expires_at"),
    )


# =============================================================================
# --- SECTION 5: COG (CONTENT) MODELS
# =============================================================================

class ContentBlob(Base):
    """
    Content-addressed storage blob.

    Storage format: Pure SHA256 hash (no extension) in MinIO
    Example: minio://tenant-1-files/abc123def456...

    The file_extension is stored as metadata for:
    - Processing tools that need to know file type
    - Download Content-Disposition headers
    - Display purposes
    """
    __tablename__ = "content_blobs"
    id = Column(String(255), primary_key=True)  # SHA256 hash of content
    tenant_id = Column(ForeignKey("tenants.id"), primary_key=True)
    size_bytes = Column(BigInteger)
    mime_type = Column(String(255))
    file_extension = Column(String(20))  # Lowercase, no dot (e.g., "png", "jpg", "exr")
    storage_path = Column(String(1024))  # Pure hash path: minio://bucket/abc123...
    cog_id = Column(ForeignKey("cogs.id"))
    # Relationships
    cog = relationship("Cog", back_populates="blobs")


class Cog(SoftDeleteMixin, Base):
    """
    Represents a folder or file in the system.
    Supports recursive folder structures via parent_id.
    """
    __tablename__ = "cogs"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    owner_id = Column(ForeignKey("users.id"))
    parent_id = Column(ForeignKey("cogs.id"), index=True)
    name = Column(String(255))
    cog_type = Column(Enum(CogType))
    custom_metadata = Column(JSONB, nullable=True)
    latest_version_id = Column(ForeignKey("versions.id"))

    # System-generated folders (created during workflow processing)
    is_system_generated = Column(Boolean, default=False, server_default='f', nullable=False)

    # Audit protection and visibility control for workflow folders
    is_locked = Column(Boolean, default=False, server_default='f', nullable=False)  # Cannot be deleted (audit trail)
    is_hidden = Column(Boolean, default=False, server_default='f', nullable=False)  # Hidden from default view

    # Data classification (CMMC/ISO 27001: AC-16)
    data_classification = Column(String(32), default='internal', server_default='internal', nullable=False)  # public, internal, confidential, restricted
    # FX.90 p2b (Q76=A) — malware quarantine: a SOFT flag on the artefact
    # that blocks downloads and workflow-run use. Never a delete (ISO 27001
    # no-hard-delete): the row stays, the flag is set by the scan gate and
    # cleared only by a platform admin (audit row ADMIN_SECURITY_OVERRIDE).
    # NULL = not quarantined; shape:
    #   {"flagged_at": iso, "signature": str|None, "source": str,
    #    "artefact": str|None, "detail": str|None}
    malware_quarantine = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Processing metadata tracking (Phase 1)
    processing_metadata = Column(JSONB, default={}, nullable=False)
    total_processing_cost = Column(Integer, default=0, nullable=False)
    last_processed_at = Column(DateTime(timezone=True), nullable=True, index=True)
    processing_count = Column(Integer, default=0, nullable=False)

    # --- Relationships ---
    tenant = relationship("Tenant", back_populates="cogs")
    owner = relationship("User", back_populates="owned_cogs", foreign_keys=[owner_id])
    parent = relationship("Cog", back_populates="children", remote_side=[id])
    children = relationship("Cog", back_populates="parent")
    versions = relationship(
        "Version",
        back_populates="cog",
        foreign_keys="[Version.cog_id]",
        order_by="Version.version_number",
        cascade="all, delete-orphan"
    )
    latest_version = relationship(
        "Version",
        foreign_keys=[latest_version_id],
        uselist=False
    )
    # Gate 30: the legacy Permission model is deleted; cog grants live in
    # ResourcePermission (reconstructed via cog_grants.list_cog_grants).

    tags = relationship("Tag", secondary=cog_tag_association, back_populates="cogs")
    blobs = relationship("ContentBlob", back_populates="cog")


class Version(Base):
    __tablename__ = "versions"
    id = Column(Integer, primary_key=True)
    cog_id = Column(ForeignKey("cogs.id"), index=True)
    tenant_id = Column(Integer)
    content_id = Column(String(255))  # Added type for clarity
    version_number = Column(Integer, index=True)
    name = Column(String(255))
    author_user_id = Column(ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # --- Relationships ---
    cog = relationship("Cog", back_populates="versions", foreign_keys=[cog_id])
    author = relationship("User")
    content = relationship("ContentBlob")
    __table_args__ = (
        ForeignKeyConstraint(['tenant_id', 'content_id'], ['content_blobs.tenant_id', 'content_blobs.id']),
        UniqueConstraint('cog_id', 'version_number', name='uq_cog_version_number'),
    )


class Tag(Base):
    __tablename__ = "tags"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), index=True)
    name = Column(String(100), index=True)
    # Relationships
    cogs = relationship("Cog", secondary=cog_tag_association, back_populates="tags")
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tenant_tag_name"),)


# =============================================================================
# --- SECTION 6: PERMISSIONS & SHARING MODELS
# =============================================================================

class CustomRole(SoftDeleteMixin, Base):
    __tablename__ = "custom_roles"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), index=True)
    name = Column(String(255))
    description = Column(Text)

    # Is this a system-defined role that can't be deleted?
    is_system_role = Column(Boolean, default=False, nullable=False)

    # Relationships
    capabilities = relationship("Capability", secondary=role_capability_association)
    resource_permissions = relationship("ResourcePermission", back_populates="role", cascade="all, delete-orphan")
    users = relationship("User", back_populates="custom_role")

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tenant_custom_role_name"),)


# =============================================================================
# --- GRANULAR PERMISSION SYSTEM
# =============================================================================

class Capability(Base):
    """
    Defines a specific action that can be performed on a resource type.

    Capabilities are system-defined and seeded at startup.
    Examples: 'view', 'download_original', 'execute', 'delete', etc.

    Each capability belongs to a resource type (cog, workflow, user, etc.)
    """
    __tablename__ = "capabilities"

    id = Column(Integer, primary_key=True)

    # Which resource type this capability applies to
    resource_type = Column(Enum(ResourceType), nullable=False, index=True)

    # Machine-readable name (e.g., "download_original", "view_logs")
    name = Column(String(100), nullable=False)

    # Human-readable display name (e.g., "Download Original Files")
    display_name = Column(String(255), nullable=False)

    # Detailed description for UI help text
    description = Column(Text)

    # Category for grouping in UI (e.g., "File Access", "Workflow Control", "Admin")
    category = Column(String(100), nullable=True)

    # Order for display in UI
    display_order = Column(Integer, default=0)

    # Is this a dangerous/admin capability that should show warnings?
    is_sensitive = Column(Boolean, default=False, nullable=False)

    # Capabilities that this one requires (e.g., 'download_original' requires 'view')
    requires_capabilities = Column(JSONB, default=[], nullable=False)

    # Unique constraint: one capability name per resource type
    __table_args__ = (
        UniqueConstraint("resource_type", "name", name="uq_capability_resource_name"),
        Index("ix_capability_resource_category", "resource_type", "category"),
    )


class PlatformCapability(Base):
    """A platform-scoped capability granted directly to a user.

    Distinct from ``Capability`` above (the node/resource action kind) and from
    the cog ``Permission`` system (resource-scoped): this is a platform-wide
    least-privilege grant — e.g. authoring persistent stacks — that a platform
    admin hands to a specific user WITHOUT conferring full ``is_platform_admin``.

    CMMC AC-5 / ISO 27001 A.9.2.3 (separation of duties). Grant and revoke are
    audited via ``audit.log_permission_audit``.

    A grant is ACTIVE when ``revoked_at IS NULL`` and (``expires_at IS NULL`` or
    ``expires_at > now()``). Revocation is non-destructive (sets ``revoked_at``)
    so the audit trail survives; re-granting inserts a new row. See
    ``permissions.check_user_platform_capability``.
    """
    __tablename__ = "platform_capabilities"

    id = Column(Integer, primary_key=True)

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)

    capability = Column(String(100), nullable=False)

    granted_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    granted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    expires_at = Column(DateTime(timezone=True), nullable=True)

    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        Index("ix_platform_capability_user_cap", "user_id", "capability"),
    )


class PrincipalType(str, enum.Enum):
    """A resource owner is one of these (authz-derived-ownership DO-4)."""
    USER = "user"
    TEAM = "team"


class ResourceOwnership(Base):
    """authz-derived-ownership DO-4 — the authoritative owner of a resource, which
    may be a USER or a TEAM (the shared-drive model). One row per resource
    (unique on resource_type+resource_id). When a row exists it WINS over the
    legacy owner pointers (`cog.owner_id` / `workflow.created_by_user_id`); when
    absent, the derived-owner rule falls back to those pointers, so existing
    resources are unaffected until explicitly (re)assigned. Ownership stays a
    single sparse fact per resource — no per-capability rows — so it is O(1) to
    set and to transfer, and the derived-owner rule expands it to capabilities at
    check time."""
    __tablename__ = "resource_ownership"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    resource_type = Column(Enum(ResourceType), nullable=False, index=True)
    resource_id = Column(Integer, nullable=False, index=True)
    principal_type = Column(Enum(PrincipalType), nullable=False)
    principal_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        UniqueConstraint("resource_type", "resource_id",
                         name="uq_resource_ownership_resource"),
        Index("ix_resource_ownership_principal", "principal_type", "principal_id"),
    )


class ResourcePermission(Base):
    """
    Grants a specific capability on a specific resource (or all resources of a type).

    This is the core of the granular permission system. A permission grant says:
    "User/Team/Role X can do Y on resource Z"

    Resource scoping:
    - resource_id = specific ID: Permission applies to that specific resource
    - resource_id = NULL: Permission applies to ALL resources of that type (wildcard)

    Examples:
    - capability='execute', resource_type='workflow', resource_id=123
      → Can execute workflow #123
    - capability='download_original', resource_type='cog', resource_id=NULL
      → Can download original files from ANY cog
    """
    __tablename__ = "resource_permissions"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)

    # Who has this permission (exactly one must be set)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    team_id = Column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=True, index=True)
    role_id = Column(ForeignKey("custom_roles.id", ondelete="CASCADE"), nullable=True, index=True)

    # What capability is granted
    capability_id = Column(ForeignKey("capabilities.id", ondelete="CASCADE"), nullable=False, index=True)

    # What resource type and specific resource (NULL = all resources of this type)
    resource_type = Column(Enum(ResourceType), nullable=False, index=True)
    resource_id = Column(Integer, nullable=True, index=True)  # NULL = wildcard (all)

    # Optional: further restrict by conditions (e.g., {"tag": "public"})
    conditions = Column(JSONB, default={}, nullable=False)

    # Expiration for time-limited permissions
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Grantee (group) — alongside user_id/team_id/role_id above. Unified-auth
    # Gate 30: legacy Permission supported keycloak-group grants; carry them here
    # so the grant model has no grantee the legacy table could express and this
    # one cannot.
    keycloak_group_id = Column(String(255), nullable=True, index=True)

    # Grant metadata (unified-authorization Gate 30). A "grant/share" is one
    # logical act that expands to several capability rows; these fields, carried
    # identically on every row of the grant, make the grant losslessly
    # reconstructable from ResourcePermission alone — replacing the legacy
    # Permission table as the sharing-grant record the UI/analytics read.
    permission_level = Column(Enum(PermissionLevel), nullable=True)  # VIEWER/EDITOR/OWNER (sharing UI)
    is_external_user = Column(Boolean, default=False, nullable=False, server_default="false")
    invitation_context = Column(JSONB, nullable=True)  # external-invitation metadata
    # FX.110 (owner decision Q97=A) — per-grant restriction options for
    # external-collaborator grants (is_external_user=True). JSON shape:
    # {"view_only": bool, "no_download": bool, "forced_watermark": bool}.
    # NULL/absent = no extra restrictions (the grant's capability level
    # still bounds what the holder can do). Enforcement:
    # services/outside_collaborators.grant_restrictions merges the MOST
    # restrictive option across every active external grant on the same
    # resource, so a single strict grant wins over a loose one. The grant
    # end date itself reuses the existing expires_at column (FX.110:
    # every external grant gets an end date at create time).
    external_restriction_options = Column(JSONB, nullable=True)

    # Audit fields
    granted_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    granted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    grant_reason = Column(Text, nullable=True)  # Why was this permission granted?

    # Status
    is_active = Column(Boolean, default=True, nullable=False, index=True)

    # Soft delete
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)
    revoked_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    revoke_reason = Column(Text, nullable=True)

    # Relationships
    tenant = relationship("Tenant")
    user = relationship("User", foreign_keys=[user_id])
    team = relationship("Team")
    role = relationship("CustomRole", back_populates="resource_permissions")
    capability = relationship("Capability")
    granted_by = relationship("User", foreign_keys=[granted_by_user_id])
    revoked_by = relationship("User", foreign_keys=[revoked_by_user_id])

    __table_args__ = (
        # Ensure exactly one of user_id, team_id, or role_id is set
        # (enforced at application level for flexibility)
        Index("ix_resource_perm_lookup", "tenant_id", "resource_type", "resource_id", "capability_id"),
        Index("ix_resource_perm_user", "user_id", "resource_type", "capability_id"),
        Index("ix_resource_perm_team", "team_id", "resource_type", "capability_id"),
        Index("ix_resource_perm_role", "role_id", "resource_type", "capability_id"),
    )


class ShareableLink(SoftDeleteMixin, Base):
    __tablename__ = "shareable_links"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    cog_id = Column(ForeignKey("cogs.id", ondelete="CASCADE"), index=True)
    created_by_user_id = Column(ForeignKey("users.id"))
    token = Column(String(64), unique=True, index=True)
    permission_level = Column(
        Enum(PermissionLevel),
        default=PermissionLevel.VIEWER,
    )
    password_hash = Column(String(255))
    expires_at = Column(DateTime(timezone=True), index=True)
    visit_count = Column(Integer, default=0)
    last_visited_at = Column(DateTime(timezone=True))
    # FX.102 (owner Q88=A) — download counter for the share.download_cap
    # tenant policy (0 = unlimited). visit_count covers link opens;
    # downloads are metered separately because the cap bounds transfers.
    download_count = Column(Integer, default=0)
    # Relationships
    tenant = relationship("Tenant")
    cog = relationship("Cog")
    creator = relationship("User")


class TenantPermissionSettings(Base):
    """
    Per-tenant configuration for permission management.

    Allows tenants to customize:
    - Warning schedule (days before expiration to send warnings)
    - Email notification settings
    - Default expiration periods for external users
    - Auto-revocation settings

    Note: Slack/Teams integrations are deprecated but kept in DB for backward compatibility.
    """
    __tablename__ = "tenant_permission_settings"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), unique=True, nullable=False, index=True)

    # Warning Schedule (in days before expiration)
    warning_schedule = Column(JSON, nullable=False, default=[7, 3, 1])  # [7d, 3d, 1d]

    # Notification Channels Configuration
    email_notifications_enabled = Column(Boolean, default=True, nullable=False)
    # DEPRECATED: Slack and Teams notifications no longer exposed in UI
    slack_notifications_enabled = Column(Boolean, default=False, nullable=False)
    slack_webhook_url = Column(String(500), nullable=True)
    teams_notifications_enabled = Column(Boolean, default=False, nullable=False)
    teams_webhook_url = Column(String(500), nullable=True)

    # Default Expiration Periods (in days)
    default_external_user_duration = Column(Integer, default=30, nullable=False)  # 30 days default
    default_editor_duration = Column(Integer, default=90, nullable=True)  # Optional limit for editors
    default_viewer_duration = Column(Integer, default=60, nullable=True)  # Optional limit for viewers

    # Auto-Revocation Settings
    auto_revoke_expired = Column(Boolean, default=True, nullable=False)
    grace_period_hours = Column(Integer, default=0, nullable=False)  # Hours after expiration before revoke

    # Admin Notification Settings
    admin_summary_enabled = Column(Boolean, default=True, nullable=False)
    admin_emails = Column(JSON, nullable=True)  # List of admin emails to notify
    admin_slack_channel = Column(String(100), nullable=True)  # DEPRECATED: No longer used in UI

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    tenant = relationship("Tenant", back_populates="permission_settings")


class SupportChatSession(Base):
    """Phase 4 — multi-turn support chat session.

    `kind` controls the system prompt + retrieval filter applied:
      "general" — standard support questions, full doc index
      "logs"    — diagnose a specific failed task (kind=logs sessions
                  pin the log_tail in their first system message and
                  scope retrieval to troubleshooting docs)
      "wizard"  — a single node/workflow build conversation
      "project" — project-agent §2.1. A long-running agent session that
                  holds one user's NEED across many builds. Its children
                  (`parent_session_id`) are the wizard conversations it
                  dispatches, which is how the agent can hold context the
                  wizards do not: the node wizard still receives only
                  expected input, the process needed, and expected output.
    """
    __tablename__ = "support_chat_sessions"

    id = Column(String(36), primary_key=True)  # uuid4
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    kind = Column(String(16), nullable=False, default="general")  # general | logs | wizard | project
    task_id = Column(String(64), nullable=True, index=True)
    parent_session_id = Column(String(36),
                                ForeignKey("support_chat_sessions.id",
                                            ondelete="SET NULL"),
                                nullable=True)
    title = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                         nullable=False)
    last_message_at = Column(DateTime(timezone=True), server_default=func.now(),
                              nullable=False, index=True)

    # Tone-aware deescalation (see the internal design notes).
    # Running aggregate of per-turn tone deltas; threshold-only comparator.
    frustration_score = Column(Integer, nullable=False, default=0, server_default="0")
    # Counts consecutive assistant turns that apologised — capped by
    # the tone-adaptive prompt's APOLOGY_CAP directive.
    consecutive_apologies = Column(Integer, nullable=False, default=0, server_default="0")
    # Set when the bot auto-flags the session for a human. The UI reads
    # this to show the "we've notified our team" banner; the existing
    # escalation pipeline picks up the transcript by session id.
    auto_escalated_at = Column(DateTime(timezone=True), nullable=True)

    # ---- project-agent §2.2 — durable project context (kind="project") ----
    #
    # These live on the SESSION ROW rather than in the transcript because
    # of design D2: they are the part that is never summarised. A project
    # running for weeks will exceed any context window, and what may not be
    # dropped is the plan, the criteria, the example, the inventory, and the
    # record of what has already been tried and failed. Summarising a
    # failure history makes the agent retry what it already knows does not
    # work, which is the failure never-stop would amplify.
    #
    # NULL on every other kind.
    project_need = Column(Text, nullable=True)
    # {"input": {"filename", "body_b64"|"ref"}, "output": {...},
    #  "described_outcome": str}. The opening ask — a representative input
    # and the required output — which also becomes BuildSpec.sample_output
    # for every node built for this project (§1.1).
    project_criteria = Column(JSONB, nullable=True)
    # Current plan. Rejected at validation if any task lacks an acceptance
    # check (§5.1), so this cannot hold a plan that omits its own tests.
    project_plan = Column(JSONB, nullable=True)
    # Superseded plan revisions, append-only, so a change of approach is
    # auditable (§5.4).
    project_plan_history = Column(JSONB, nullable=False, default=list,
                                  server_default="[]")
    # What has been built for this project: node definitions, workflows,
    # stacks. Tenant-owned without exception (§8).
    project_artifacts = Column(JSONB, nullable=False, default=list,
                               server_default="[]")
    # Per-task record of approaches already tried and why each failed.
    # {task_key: [{approach, rung, failed_at, reason}]}.
    project_failure_history = Column(JSONB, nullable=False, default=dict,
                                     server_default="{}")
    project_status = Column(String(24), nullable=True)  # active|parked|complete
    project_completed_at = Column(DateTime(timezone=True), nullable=True)

    # ---- conversational-project-builder §1.4 — man-in-the-middle gate ----
    #
    # A DISTINCT axis from project_status. project_status (active|parked|
    # complete) is the autonomous project-agent's own lifecycle; this gate
    # is the conversational builder's collaboration state, driving the
    # align→build→checkpoint loop where the human is the checkpoint at every
    # meaningful step:
    #
    #   aligning      — conversation only; NO checklist/dispatch yet (the
    #                   "align before build" guard).
    #   building      — a checklist exists and a task's unit is being built.
    #   awaiting-user — a checkpoint is handed off; the run is paused waiting
    #                   for the user to test + give feedback. Durable, NOT a
    #                   timeout-to-failure (survives a manager restart).
    #   done          — every checklist task accepted; project complete.
    #
    # NULL on every legacy one-shot project and every non-project session;
    # only set when PROJECT_CONVERSATION_ENABLED is on for the project.
    project_gate_state = Column(String(24), nullable=True)


class SupportChatMessage(Base):
    __tablename__ = "support_chat_messages"

    # BigInteger on Postgres (real prod target) but Integer on SQLite so
    # the test suite's in-memory engine can autoincrement primary keys.
    # SQLite stores all integer columns as 8-byte int internally so this
    # is purely a column-type rendering switch, not a data-shape change.
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    session_id = Column(String(36),
                         ForeignKey("support_chat_sessions.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    role = Column(String(16), nullable=False)   # user | assistant | system
    content = Column(Text, nullable=False)
    sources_json = Column(JSON, nullable=True)  # list of {title, url}
    token_usage = Column(JSON, nullable=True)
    # Tone-aware deescalation — set only on user messages, NULL otherwise.
    # One of HAPPY / NEUTRAL / FRUSTRATED / ANGRY / ABUSIVE (see
    # utils.support_chat_tone.ToneLabel).
    tone = Column(String(16), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                         nullable=False, index=True)


class WizardTestRun(Base):
    """workflow-agentic-execution-loop §wizard-agentic-loop — one row per
    iteration of the wizard's compose-test-critique loop. Records the
    candidate graph that iteration produced, the test input selected,
    the workflow_run_id that was dispatched, and the output critic's
    verdict + reason. Successive iterations of a single wizard turn
    share the wizard_session_id and have monotonically-increasing
    iteration_n starting at 0. Powers the lineage-chip surfaced on the
    final workflow, and feeds wizard_ml.training in Moat 1 extension.
    """
    __tablename__ = "wizard_test_runs"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    wizard_session_id = Column(String(36),
                               ForeignKey("support_chat_sessions.id",
                                          ondelete="CASCADE"),
                               nullable=False, index=True)
    iteration_n = Column(Integer, nullable=False)
    # Snapshot of the candidate graph this iteration produced.
    candidate_graph_json = Column(JSONB, nullable=False)
    # Descriptor of the test input selected for this iteration (tier,
    # source pointer, generated-standin flag). Schema documented in
    # services.wizard_test_input.
    test_input_descriptor = Column(JSONB, nullable=True)
    # The sandboxed workflow_run that was dispatched. Null when the
    # iteration terminated before dispatch (clarification escape).
    workflow_run_id = Column(BigInteger, nullable=True, index=True)
    output_critic_verdict = Column(String(32), nullable=True)
    output_critic_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    __table_args__ = (
        # Iteration numbers must be unique within a session.
        UniqueConstraint("wizard_session_id", "iteration_n",
                          name="uq_wizard_test_runs_session_iter"),
        Index("ix_wizard_test_runs_session_iter",
              "wizard_session_id", "iteration_n"),
    )


class NodeOutputCache(Base):
    """workflow-agentic-execution-loop §workflow-node-output-cache —
    content-addressable cache of node outputs. Keyed by
    `(image_digest, input_content_hash, config_hash, tenant_id)` per
    the strict-per-tenant isolation contract. Cache reads short-circuit
    dispatch when the per-tenant `wizard_output_cache_enabled` flag is
    on; cache writes happen always-on so flipping the read flag picks
    up accumulated cache immediately.
    """
    __tablename__ = "node_output_cache"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    image_digest = Column(String(80), nullable=False)
    input_content_hash = Column(String(64), nullable=False)
    config_hash = Column(String(64), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    output_pointer = Column(String(512), nullable=False)
    output_content_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    last_accessed_at = Column(DateTime(timezone=True),
                               server_default=func.now(), nullable=False,
                               index=True)
    __table_args__ = (
        UniqueConstraint("image_digest", "input_content_hash",
                          "config_hash", "tenant_id",
                          name="uq_node_output_cache_key"),
    )


class TenantSlackConfig(Base):
    """
    Phase 2 — per-tenant Slack notifications.

    Each tenant supplies its own incoming-webhook URL (stored as a
    Secret row, referenced here by id) and picks which platform events
    fan out to that webhook. Supports the same encrypted-secret +
    Vault path as Stripe / Harbor / SMTP credentials.

    Subscribed-events shape:
      ["workflow.completed", "workflow.failed", "payment.success",
       "payment.failed", "port.build_failed", "port.smoke_drift",
       "license.warning"]
    """
    __tablename__ = "tenant_slack_configs"

    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
                        primary_key=True)
    # Reference to the encrypted webhook URL in the secrets table.
    # Nullable so a config row can exist with `enabled=false` and no
    # URL yet (admin in the UI before pasting the URL).
    webhook_url_secret_id = Column(Integer,
                                    ForeignKey("secrets.id", ondelete="SET NULL"),
                                    nullable=True)
    default_channel = Column(String(80), nullable=True)  # optional override
    enabled = Column(Boolean, default=False, nullable=False)
    subscribed_events = Column(JSON, default=list, nullable=False)
    last_test_at = Column(DateTime(timezone=True), nullable=True)
    last_test_status = Column(String(32), nullable=True)  # ok | error | pending

    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                         nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                         onupdate=func.now(), nullable=False)

    tenant = relationship("Tenant")
    webhook_secret = relationship("Secret", foreign_keys=[webhook_url_secret_id])


class PermissionTemplate(SoftDeleteMixin, Base):
    """
    Reusable permission templates for common access patterns.

    Examples:
    - "External Consultant" template: Editor for 30 days, no share permission
    - "Temp Viewer" template: Viewer for 7 days
    - "Project Collaborator" template: Editor for 90 days, can share
    """
    __tablename__ = "permission_templates"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)

    # Template Info
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    # Permission Configuration
    permission_level = Column(String(20), nullable=False)  # viewer, editor, owner
    duration_days = Column(Integer, nullable=True)  # None = no expiration

    # Capability Overrides
    can_upload = Column(Boolean, nullable=True)
    can_create_subfolders = Column(Boolean, nullable=True)
    can_move_files = Column(Boolean, nullable=True)
    can_process = Column(Boolean, nullable=True)
    can_share = Column(Boolean, nullable=True)
    can_delete = Column(Boolean, nullable=True)

    # Usage Tracking
    times_used = Column(Integer, default=0, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    # Metadata
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    tenant = relationship("Tenant", back_populates="permission_templates")
    created_by = relationship("User")


class PermissionRequestStatus(str, enum.Enum):
    """Status of a permission request."""
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"


class PermissionRequest(Base):
    """
    Permission Request Model

    Allows users to request access to folders they don't currently have permissions for.
    Admins/owners can approve or deny these requests.
    """
    __tablename__ = "permission_requests"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)

    # Requester information
    requester_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # What they're requesting
    # cog_id kept for backward-compat with existing cog requests; nullable now
    # that requests are generalized. New requests also set resource_type +
    # resource_id ('cog' mirrors cog_id; 'workflow' uses resource_id).
    cog_id = Column(Integer, ForeignKey("cogs.id"), nullable=True, index=True)
    resource_type = Column(String(20), nullable=False, default="cog", server_default="cog", index=True)
    resource_id = Column(Integer, nullable=True, index=True)
    requested_level = Column(
        Enum(PermissionLevel),
        nullable=False,
    )
    requested_duration_days = Column(Integer, nullable=True)  # None = permanent

    # Request details
    justification = Column(Text, nullable=True)  # Why they need access
    status = Column(Enum(PermissionRequestStatus), default=PermissionRequestStatus.PENDING, nullable=False, index=True)

    # Approval/Denial information
    reviewed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_notes = Column(Text, nullable=True)  # Admin's notes on approval/denial

    # If approved, track the created permission
    # Gate 30: the legacy `permissions` table is retired. This now holds the
    # read-model grant id (a representative ResourcePermission.id), so it is a
    # plain int, not an FK.
    granted_permission_id = Column(Integer, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    tenant = relationship("Tenant")
    requester = relationship("User", foreign_keys=[requester_id])
    cog = relationship("Cog")
    reviewer = relationship("User", foreign_keys=[reviewed_by_user_id])
    # Gate 30: granted_permission_id now holds a read-model grant id (a
    # ResourcePermission.id), not a legacy permissions.id — no ORM relationship.


# In API/models.py

class Invitation(SoftDeleteMixin, Base):
    __tablename__ = "invitations"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    invited_by_user_id = Column(ForeignKey("users.id"))
    custom_role_id = Column(ForeignKey("custom_roles.id"))
    assigned_role = relationship("CustomRole")
    email = Column(String(255), index=True)
    token = Column(String(64), unique=True, index=True)
    status = Column(Enum(InvitationStatus), default=InvitationStatus.PENDING, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # --- THIS IS THE NEW LOGIC ---
    # Store the initial permissions directly on the invitation.
    # These are nullable because an invitation might not grant specific folder access.
    initial_cog_id = Column(Integer, ForeignKey("cogs.id"), nullable=True)
    initial_permission_template_id = Column(Integer, ForeignKey("permission_templates.id"), nullable=True)
    initial_permission_level = Column(
        Enum(PermissionLevel),
        nullable=True,
    )  # Deprecated in favor of templates
    initial_cog = relationship("Cog")
    initial_template = relationship("PermissionTemplate")
    # --- END NEW LOGIC ---

    accepted_at = Column(DateTime(timezone=True), nullable=True)
    extra_metadata = Column("metadata", JSONB, nullable=True)

    # Relationships
    tenant = relationship("Tenant")
    inviter = relationship("User", foreign_keys=[invited_by_user_id])
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_tenant_pending_invitation"),)


# =============================================================================
# --- SECTION 7: WORKFLOW & AUTOMATION MODELS
# =============================================================================

class Workflow(SoftDeleteMixin, Base):
    __tablename__ = "workflows"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), index=True)
    created_by_user_id = Column(ForeignKey("users.id"))
    name = Column(String(255))
    latest_version_id = Column(ForeignKey("workflow_versions.id"))
    # Workflows the platform creates for its own bookkeeping — Node
    # Wizard build pipelines, HarborScanFollowup, etc. — get this flag
    # so the load-workflow picker can hide them from end users by
    # default. Platform admins can opt back in via `include_system=true`
    # on GET /workflows.
    is_system = Column(Boolean, default=False, server_default='f', nullable=False)
    # node-run-controls — "pause schedule" (DAG pause): when true the schedule +
    # trigger evaluators start NO new runs of this workflow. Distinct from per-node
    # pause and from an in-progress run (a running run keeps running). Reversible.
    schedule_paused = Column(Boolean, default=False, server_default='f', nullable=False)
    # Archive = soft-hide, distinct from soft-delete. Setting archived_at hides
    # the workflow from the default Load-workflow picker without deleting it
    # (deleted_at stays reserved for the "delete entire workflow" action).
    # Reversible: unarchive clears the column. NULL = visible (default).
    archived_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # add-multi-region-mesh — the region this workflow belongs to. A trigger that
    # fires elsewhere for this workflow is relayed to the manager that owns this
    # region. NULL = single-region / local (unchanged behavior).
    home_zone = Column(String(100), nullable=True, index=True)

    # GG.6 (gg-commerce) — set on a workflow CLONED by
    # services/workflow_listing_service.install_workflow from a marketplace
    # WorkflowListing. NULL for every ordinary (non-installed) workflow.
    # SET NULL (not CASCADE): the buyer's clone is self-contained (its own
    # WorkflowNode/WorkflowVersion rows) and must survive the listing being
    # removed later; update_available() treats a NULL source_listing_id as
    # "not an installed workflow" and always returns False.
    source_listing_id = Column(ForeignKey("workflow_listings.id", ondelete="SET NULL"), nullable=True, index=True)
    source_version = Column(Integer, nullable=True)

    # burst-to-saas 3.1 — consent to spill THIS workflow to MOD.
    #
    # On the workflow rather than the tenant because this is where someone
    # knows what data flows through it (design D2). Defaults FALSE for
    # existing and new workflows alike: no workflow becomes eligible because
    # somebody flipped a switch elsewhere.
    #
    # `_by` and `_at` are the accountability that replaces a second approver
    # (design D10). Requiring two makes the feature unusable for a two-person
    # shop; recording who decided answers the question a second approver
    # exists to answer.
    burst_eligible = Column(Boolean, default=False, server_default='f', nullable=False)
    burst_enabled_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    burst_enabled_at = Column(DateTime(timezone=True), nullable=True)
    latest_version = relationship(
        "WorkflowVersion",
        foreign_keys=[latest_version_id],
        post_update=True,
        uselist=False
    )
    # Relationships
    # Disambiguated: `burst_enabled_by_user_id` added a SECOND foreign key
    # path to users, so the join is no longer inferable.
    creator = relationship("User", foreign_keys=[created_by_user_id])
    versions = relationship(
        "WorkflowVersion",
        back_populates="workflow",
        cascade="all, delete-orphan",
        foreign_keys="[WorkflowVersion.workflow_id]"
    )
    runs = relationship("WorkflowRun", back_populates="workflow", cascade="all, delete-orphan")
    triggers = relationship("WorkflowTrigger", back_populates="workflow", cascade="all, delete-orphan")
    # Gate 30: the legacy Permission model is deleted; grants live in ResourcePermission.
    nodes = relationship("WorkflowNode", back_populates="workflow")
    # GX.D1 — workflow-level worker/pool placement:
    # {"worker_id": int} (a specific worker_licenses.id) and/or
    # {"worker_tag": str} (any worker whose hardware_tags include it).
    # Every task of every run of this workflow is claimable only by matching
    # workers; precedence run > node > workflow is resolved at dispatch
    # (services.workflow_orchestrator + routers.worker_tasks._placement_*).
    # NULL = unpinned (today's behaviour).
    placement = Column(JSONB, nullable=True)

    # sz-mcp-per-workflow (mcp-fleet §per-workflow) — opt-in: this workflow is
    # published as an MCP tool for its tenant. The flag is the intent switch
    # only; the actual exposure rows live in WorkflowMcpExposure and the
    # router wiring is sz-mcp-per-workflow.2/3 (NOT wired here). Default false
    # for every existing and new workflow: nothing becomes MCP-published
    # because somebody flipped a switch elsewhere.
    publish_mcp = Column(Boolean, default=False, server_default='f', nullable=False)
    # wep1.a (workflow-expressiveness-primitives phase 1) — declared workflow
    # parameters [{name, type, default?, required, description?}]; NULL = none.
    # Migration 20261011j_workflow_parameters_column.
    parameters = Column(JSONB, nullable=True)
    # expr-1.3 (456) — inheritance + interface; migration 20261012k.
    extends_workflow_id = Column(ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True)
    implements = Column(String(64), nullable=True)
    # FORTRESS 4.1 — compliance profile tags; migration 20261012p (TEXT[] NOT NULL DEFAULT '{}').
    compliance_tags = Column(PG_ARRAY(Text), nullable=False, server_default='{}', default=list)
    # expr-10.1 — differentiable-workflow flag; migration 20261013k_workflow_differentiable_column.
    differentiable = Column(Boolean, nullable=False, default=False, server_default='f')
    # 30.6.9 c9-J1 — per-tenant upsert key for PUT /v1/road/by-slug/{slug}.
    # NULL on every pre-migration row (and on workflows the caller never slugs);
    # uniqueness is ONLY within a tenant (uq_workflows_tenant_slug, partial), so
    # another tenant holding the same slug is invisible here -- no cross-tenant
    # oracle. migration 20261027d_workflow_slug_graph_hash.
    slug = Column(String(150), nullable=True)

    __table_args__ = (
        Index("uq_workflows_tenant_slug", "tenant_id", "slug",
              unique=True, postgresql_where=text("slug IS NOT NULL")),
    )


class WorkflowTemplate(Base):
    """P-wizard B — starter workflow scaffolds users can clone.

    Two scopes mirror NodeDefinition:
      - tenant_id NULL → curated platform-wide templates (seeded at boot)
      - tenant_id set  → tenant-private template (rare; not exposed yet)
    `category` is a free-form string used to group the gallery (e.g.
    "3D Reconstruction", "Image Processing"). `graph_json` follows the
    same `{nodes:[...], edges:[...]}` shape as WorkflowVersion.graph_json
    so cloning is a straight copy + version create.
    """
    __tablename__ = "workflow_templates"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=True, index=True)
    name = Column(String(160), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(80), nullable=True, index=True)
    graph_json = Column(JSONB, nullable=False)
    # Optional ordered preview of node names — lets the gallery render a
    # "what this does" pill row without inflating graph_json on every
    # list call.
    preview_nodes = Column(JSONB, nullable=True)
    # Lowest plan tier eligible to clone this template. Validated against
    # the same plan_required convention used in features.md.
    plan_required = Column(String(40), nullable=True)
    is_curated = Column(Boolean, default=False, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_workflow_template_tenant_name"),
        Index("ix_wfx_template_curated", "is_curated"),
    )


class WorkflowVersion(SoftDeleteMixin, Base):
    __tablename__ = "workflow_versions"
    id = Column(Integer, primary_key=True)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    created_by_user_id = Column(ForeignKey("users.id"))
    version_number = Column(Integer, index=True)
    graph_json = Column(JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # AI scheduler — priority class drives placement algorithm (interactive uses
    # warmth-score lightweight path; normal/high/low go through HEFT + 2-opt).
    priority_class = Column(String(20), nullable=False, default="normal", server_default="normal")
    # Opt-in pre-warm hint for interactive workflows. When true, packer biases
    # toward keeping container image cached on N eligible workers.
    prewarm = Column(Boolean, nullable=False, default=False, server_default="false")
    # 30.6.9 c9-J1 — sha256 hex of the canonical graph (subgraph_canonical.canonical_hash).
    # NULL on pre-migration rows: those hash on the fly from graph_json when a caller
    # needs to compare; the hash is written only on NEW versions, never backfilled.
    graph_hash = Column(String(64), nullable=True)
    trigger_rules = relationship("WorkflowTriggerRule", back_populates="workflow_version")
    # Relationships
    workflow = relationship("Workflow", back_populates="versions", foreign_keys=[workflow_id])
    creator = relationship("User")
    __table_args__ = (UniqueConstraint("workflow_id", "version_number", name="uq_workflow_version_number"),)


class WorkflowRun(SoftDeleteWithActorMixin, Base):
    __tablename__ = "workflow_runs"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    workflow_id = Column(ForeignKey("workflows.id"), index=True)
    workflow_version_id = Column(ForeignKey("workflow_versions.id"), index=True)
    triggered_by_user_id = Column(ForeignKey("users.id"))
    triggered_by_trigger_id = Column(ForeignKey("workflow_triggers.id"))
    # project-agent §3.1 — the project this run was dispatched for.
    #
    # WorkflowRunEvent hangs off a single workflow_run_id, so an agent that
    # builds node A, builds node B, runs the pipeline, repairs B and runs it
    # again produced five runs with nothing recording they were one effort.
    # This is the missing rung: project -> run -> node -> event -> stdout.
    # The lower four already existed.
    #
    # Distinct from triggered_by_user_id, which stays the AUTHORITY the run
    # acts under (D10). This says what caused it, the way
    # triggered_by_trigger_id does for schedules and webhooks.
    # burst-to-saas slice 4 — where this run executed, and what it cost to
    # send. Written for LOCAL runs too: `burst_stay_local_reason` is what makes
    # "why didn't this burst?" answerable from the run record instead of by
    # reading logs.
    #
    # This is the adoption gate, not observability polish (design D5). A
    # customer with compliance obligations cannot EVALUATE burst unless they
    # can prove afterwards which jobs left. "We can tell you exactly which
    # ones, and when" is the difference between a feature they can assess and
    # one their auditor bans on principle.
    #
    # Kept on the run rather than a side table so it survives burst being
    # disabled afterwards -- the record must outlive the feature being
    # switched off, or the export has holes exactly where someone is auditing.
    ran_remotely = Column(Boolean, default=False, server_default='f', nullable=False)
    burst_remote_run_id = Column(String(128), index=True)
    burst_submitted_at = Column(DateTime(timezone=True))
    burst_bytes_sent = Column(BigInteger)
    burst_amount_charged = Column(Numeric(12, 4))
    burst_remote_failure_reason = Column(Text)
    burst_stay_local_reason = Column(String(48))


    project_session_id = Column(
        String(36), ForeignKey("support_chat_sessions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    status = Column(Enum(TaskStatus), default=TaskStatus.QUEUED, index=True)
    orchestrator_run_id = Column(String(250), unique=True)
    run_name = Column(String(500), nullable=True)  # e.g., "My Workflow v3 - photo1.jpg, photo2.jpg"
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    parameters = Column(JSONB)
    # manager-work-loans (GC.MWL.7b) — a STABLE, secret-stripped snapshot of
    # the run's execution plan (nodes/edges/project_info/tenant_id/
    # tenant_bucket — the same shape services.workflow_orchestrator.
    # start_workflow_execution takes as `conf`), captured ONCE at run
    # creation (crud.workflow_cud.create_workflow_run). Distinct from
    # `parameters` above: that column holds the SAME conf shape but gets
    # MUTATED throughout the run's life (execution_plan / cancelled_by /
    # restart_history all live there), so it cannot be trusted as a stable
    # plan to reload — NOT because it carries a secret (GC.MWL.7c: as of
    # 2026-09-30 neither column ever stores `keycloak_token` / other
    # credential-shaped keys; both are stripped at write time in
    # crud.workflow_cud, see `_execution_conf_snapshot`). A work-loan
    # borrower manager reloads THIS column to dispatch a loaned run locally
    # (services/mesh_loans.py `_dispatch_loan_run`). Nullable: legacy rows
    # predating this column, or any run-creation path that doesn't set it,
    # simply cannot be loan-dispatched — accept_loan refuses with a clear
    # 409 rather than crashing on a missing plan.
    execution_conf = Column(JSONB, nullable=True)
    encrypted_refresh_token = Column(LargeBinary, nullable=True)  # Encrypted Keycloak refresh token for secret access
    # Error detail from the orchestrator when status=FAILED. Populated by
    # services.workflow_orchestrator.start_workflow_execution's except
    # branch. Without this column the dashboard could only show "failed"
    # with zero info on WHY — operators had to dig through API logs.
    error_message = Column(Text, nullable=True)
    # Trigger chain depth — incremented when a workflow_completion trigger
    # spawns this run from a parent run. Zero for user-initiated /
    # webhook / file-event / log-pattern / queue-empty / scheduled runs.
    # Cap enforced in services.workflow_orchestrator.dispatch_trigger_run.
    chain_depth = Column(Integer, nullable=False, default=0, server_default='0')

    # Gate 7 — what this run actually cost, and what it bills at.
    #
    # Written by services/run_costing.py when the run finishes, from the
    # DURATION each task occupied a worker times that worker's rate, plus
    # manager overhead. Both are kept: the billed figure alone cannot answer
    # "are we making money on this", which is the question the exercise exists
    # for. NULL means not yet costed — never free.
    cost_cents = Column(Integer, nullable=True)
    billed_cents = Column(Integer, nullable=True)
    # LDF-4 §7 — capacity-queued flag. When the dispatch handler hits
    # plan.max_concurrent_processes it persists the run with
    # status=QUEUED + is_capacity_queued=True instead of dispatching.
    # The scheduler tick in services.scheduler scans for these and
    # dispatches the oldest one when an in-flight slot frees up.
    # Distinguishes "waiting for the orchestrator to pick this up"
    # (the brief gap between handler-commit and the background-task
    # start_workflow_execution call, is_capacity_queued=False) from
    # "waiting for capacity" (is_capacity_queued=True).
    is_capacity_queued = Column(Boolean, nullable=False, default=False, server_default='false', index=True)
    capacity_queue_reason = Column(String(200), nullable=True)
    # Airflow-style mid-run pause: when True, the DAG-advance
    # (_check_and_dispatch_downstream) HOLDS — nodes already running finish, but
    # no new downstream nodes are enqueued. Resume clears it and re-kicks the
    # frontier. Does not touch run.status (a paused run is still RUNNING).
    paused = Column(Boolean, nullable=False, default=False, server_default='false')
    # Per-node pause: graph_node_ids that HOLD when the run reaches them — the
    # node is not enqueued even when its parents are done, so the run stops and
    # waits AT that node (other independent branches keep going). Resume-node
    # removes the id and re-kicks the frontier. This is the primary pause UX.
    paused_nodes = Column(JSONB, nullable=True)
    # node-run-controls — "cancel downstream after current" (drain): graph_node_ids
    # whose downstream must NOT be dispatched once the node's in-flight task
    # finishes. Distinct from paused_nodes (which holds BEFORE a node runs); drain
    # lets the current node complete, then stops everything past it.
    drain_nodes = Column(JSONB, nullable=True)
    # node-run-controls — "re-run node + downstream": graph_node_ids that must be
    # RE-DISPATCHED even though a prior task already exists for them. Consumed +
    # cleared one node at a time by _check_and_dispatch_downstream as the re-run
    # cascade advances in dependency order (so downstream re-runs with fresh
    # inputs). Distinct from a fresh run — prior attempts are preserved.
    rerun_nodes = Column(JSONB, nullable=True)
    # AI scheduler — priority class inherited from WorkflowVersion at run creation,
    # mutable per-run for one-off priority overrides (e.g. admin re-prioritising
    # a stuck batch run to high). Drives the packer's algorithm selection.
    priority_class = Column(String(20), nullable=False, default="normal", server_default="normal", index=True)
    # Critical-path ETA stamped at run creation by workflow_orchestrator from the
    # ML predictor; recomputed on every NodeRun status change with actuals
    # replacing predictions on completed nodes. Drives the ETA badge in
    # WorkflowProgress.tsx + Dashboard logs detail modal.
    estimated_duration_seconds = Column(Integer, nullable=True)
    # Sum of input file bytes (Cog.size_bytes) + serialized extra_variables.
    # Continuous numeric feature for the TFT model — replaces the categorical
    # size_tier as the load-bearing input-size signal.
    input_size_bytes = Column(BigInteger, nullable=True)
    # workflow-agentic-execution-loop §workflow-run-execution — tags the
    # run's class. 'production' (default) is today's behavior; sandbox
    # values include 'wizard_test_run', and future values will include
    # 'event_triggered', 'training_step', 'autonomous_canary',
    # 'adversary_dispatch'. Read by the orchestrator at dispatch time to
    # route output bucket prefixes + apply sandbox limits.
    purpose = Column(String(32), nullable=False, default="production",
                     server_default="production", index=True)
    # node-trial-runs — when purpose='node_trial', these mark the try-before-buy
    # run: which node is being trialed (the ONLY node exempt from the ownership
    # paywall in this run) and the input source. Charge rule: demo data that runs
    # LOCAL is free (skip debit); anything that bursts to cloud, or uses the
    # tenant's own data, is charged normally via the credits path — bursting is
    # already credit-gated, so there is no separate no-burst restriction.
    trial_node_definition_id = Column(ForeignKey("node_definitions.id", ondelete="SET NULL"), nullable=True)
    trial_data_source = Column(String(10), nullable=True)  # 'demo' | 'own'
    # Relationships
    tenant = relationship("Tenant")
    workflow = relationship("Workflow", back_populates="runs")
    workflow_version = relationship("WorkflowVersion")
    node_runs = relationship("NodeRun", back_populates="workflow_run", cascade="all, delete-orphan")
    run_files = relationship("WorkflowRunFile", back_populates="workflow_run", cascade="all, delete-orphan")


class WorkflowRunFile(SoftDeleteMixin, Base):
    __tablename__ = "workflow_run_files"
    id = Column(Integer, primary_key=True)
    workflow_run_id = Column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    file_id = Column(ForeignKey("files.id"), nullable=False, index=True)
    title = Column(String(255), nullable=False)  # <-- ADD THIS LINE
    status = Column(Enum(TaskStatus), default=TaskStatus.QUEUED, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    parameters = Column(JSONB)
    # Relationships
    workflow_run = relationship("WorkflowRun", back_populates="run_files")
    file = relationship("File")

    # This composite unique constraint ensures a file can only be in a specific
    # workflow run once, preventing accidental duplicates.
    __table_args__ = (UniqueConstraint("workflow_run_id", "file_id", name="uq_workflow_run_file"),)


class WorkflowTrigger(SoftDeleteMixin, Base):
    __tablename__ = "workflow_triggers"
    id = Column(Integer, primary_key=True)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    tenant_id = Column(ForeignKey("tenants.id"), index=True)
    created_by_user_id = Column(ForeignKey("users.id"))
    trigger_type = Column(Enum(WorkflowTriggerType))
    configuration = Column(JSONB)  # {"cron": "0 9 * * 1", "timezone": "UTC"} for schedule
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    trigger_count = Column(Integer, default=0, nullable=False)
    # Relationships
    workflow = relationship("Workflow", back_populates="triggers")
    creator = relationship("User")


class TriggerEventLog(Base):
    """add-workflow-triggers §1.4 — backs the evaluation pipeline's dedup,
    rate-limit, debounce, and composite/fan-in arrival state. One row per
    observed event (or per constituent arrival for composites)."""
    __tablename__ = "trigger_event_log"
    id = Column(BigInteger, primary_key=True)
    trigger_id = Column(ForeignKey("workflow_triggers.id", ondelete="CASCADE"), index=True, nullable=False)
    tenant_id = Column(ForeignKey("tenants.id"), index=True, nullable=True)
    event_key = Column(String(255), index=True, nullable=False)  # dedup / arrival key
    fired_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    dedupe_until = Column(DateTime(timezone=True), nullable=True)
    state = Column(JSONB, nullable=True)  # composite arrivals, debounce timer, etc.

    __table_args__ = (
        Index("ix_trigger_event_log_trigger_key", "trigger_id", "event_key"),
    )


class TriggerDeadLetter(Base):
    """expressiveness 7.12 — the `event_dispatch_dead_letter` sink.

    One row per trigger fire that exhausted its retry budget (transient
    dispatch failure, at-least-once delivery). Append-only per ISO 27001
    A.18.1.3: a row is never deleted and never rewritten — a replay only
    stamps `replayed_at` / `replayed_run_id`, so the original failure
    evidence survives. No soft-delete tombstone, no updated_at mutation
    columns; the append-only shape is what 7.13's dead-letter test reads.
    """
    __tablename__ = "trigger_dead_letters"
    # BIGSERIAL-style PK (models.py BigInteger + DB-side BIGSERIAL in the
    # migration); never a max()-subquery DEFAULT.
    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), index=True, nullable=True)
    trigger_id = Column(ForeignKey("workflow_triggers.id"), index=True,
                        nullable=False)
    payload = Column(JSONB, nullable=True)      # the event context as received
    error = Column(Text, nullable=True)         # last error text
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    first_failed_at = Column(DateTime(timezone=True), nullable=False,
                             server_default=func.now(), index=True)
    last_failed_at = Column(DateTime(timezone=True), nullable=False,
                            server_default=func.now())
    replayed_at = Column(DateTime(timezone=True), nullable=True, index=True)
    replayed_run_id = Column(Integer, nullable=True)  # no FK: runs may be tombstoned

    __table_args__ = (
        Index("ix_trigger_dead_letters_tenant_trigger", "tenant_id", "trigger_id"),
        Index("ix_trigger_dead_letters_unreplayed", "trigger_id", "replayed_at"),
    )


class NodeDefinition(SoftDeleteWithActorMixin, Base):
    __tablename__ = "node_definitions"
    # Tiered-pricing columns are server-defaulted and may not exist in the DB
    # yet; do not fetch server defaults back via RETURNING after INSERT.
    __mapper_args__ = {"eager_defaults": False}
    id = Column(Integer, primary_key=True)
    # Name is unique per-tenant, not globally. Two tenants each having a
    # copy of "Image Converter" is expected — the whole point of tenant_cud
    # cloning system nodes into tenant rows. System nodes (tenant_id IS NULL)
    # still need globally-unique names, which is enforced by a partial
    # unique index defined in __table_args__ below.
    name = Column(String(100))
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)  # NULL = system node (all tenants)
    # Visibility scoping (P-wizard):
    #   user_id NULL                    → tenant-shared (when tenant_id set) or
    #                                     system-shared (when both NULL).
    #   user_id set + tenant_id set     → user-private node, only visible to
    #                                     this user inside this tenant. Lets a
    #                                     user iterate on a node via the Node
    #                                     Wizard before promoting it for the
    #                                     whole tenant.
    # The list endpoint applies the same dedup-by-name suppression: a private
    # row hides a tenant-shared twin, a tenant row hides a system twin.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    description = Column(Text)
    type = Column(String, index=True, nullable=False, default="General")
    executable_path = Column(String(255))
    input_schema = Column(JSONB)
    output_schema = Column(JSONB)
    configuration_schema = Column(JSONB)
    custom_metadata = Column(JSONB, nullable=True)
    process_type = Column(String(255))
    version = Column(Integer, default=0, nullable=False)
    cost_in_credits = Column(Integer, default=0, nullable=False)
    # Tiered pricing (migrations/20260513_tiered_credit_pricing.py). deferred()
    # keeps them out of default SELECTs so reads stay safe on a DB where that
    # migration has not been applied yet.
    cost_credits_by_tier = deferred(Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")))
    cost_basis = deferred(Column(String(16), nullable=False, server_default="estimated"))
    regression_coefficients = deferred(Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")))
    tenant = relationship("Tenant")
    node_kind = Column(String(250), nullable=True)
    batch_mode = Column(String(50), default="batch", nullable=False)
    accepted_formats = Column(JSONB, nullable=True)  # e.g., ["jpg", "png", "tif"] — null means accept all
    gpu_variants = Column(JSONB, nullable=True)  # e.g., {"cuda": "img:latest", "rocm": "img:latest-rocm", "cpu": "img:latest-cpu"}
    allowed_egress = Column(JSONB, nullable=True)  # #87: Allowed outbound domains, e.g. ["api.openai.com", "*.amazonaws.com"]. Null = no egress.

    # Container image size (counts against tenant storage quota)
    image_size_bytes = Column(BigInteger, default=0, nullable=False, server_default='0')

    # node-code-edit-rebuild §1 — lineage for copy/fork. When a tenant copies or
    # edits (forks) another node — including a MOD-authored system node — the new
    # tenant-owned node records the origin here; the original is never mutated.
    forked_from_node_definition_id = Column(
        Integer, ForeignKey("node_definitions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    source_origin = Column(String(20), nullable=True)  # generated|copied|forked|edited

    # workflow-agentic-execution-loop §port-type-enforcement — when
    # TRUE this NodeDefinition is eligible for the wizard to auto-insert
    # between two ports whose types mismatch but whose (out_type ->
    # in_type) matches this node's (input_type -> output_type).
    is_adapter = Column(
        Boolean, default=False, nullable=False, server_default='false',
    )

    # Container security scanning (SaaS mode)
    scan_status = Column(String(20), default="approved", nullable=False, index=True)  # pending_scan, scanning, approved, rejected, failed
    scan_result = Column(JSONB, nullable=True)  # Trivy scan output: CVE list, severity counts
    scanned_at = Column(DateTime(timezone=True), nullable=True)
    rejection_reason = Column(Text, nullable=True)

    # Calibration (ML scheduling — Pro+ SaaS or licensed on-prem)
    calibration_status = Column(String(20), default="uncalibrated", nullable=False)  # uncalibrated, calibrating, calibrated, stale
    calibration_progress = Column(JSONB, nullable=True)  # {"completed": 5, "total": 9}
    last_calibrated_at = Column(DateTime(timezone=True), nullable=True)

    # Some nodes are categorically unsuitable for the calibration
    # matrix: (a) system / admin-only one-shots that aren't part of any
    # user workflow (LLM Generate Strategy, ML Retrain), or (b)
    # trivially fast file-handler nodes whose resource consumption is
    # below the noise floor (Image / Text / Resource Input). Flagging
    # them here keeps the coordinator from picking them up; the
    # scheduler has no useful signal to extract for these rows.
    excluded_from_calibration = Column(Boolean, default=False, server_default='f', nullable=False)

    # §5 — user-nominated drift reference. The variant whose
    # sample-test output non-hero variants are scored against in
    # `node_variant_verification`. Defaults to "hero". A
    # PATCH /node_definitions/{id}/reference-variant call recomputes
    # drift scores in place against the new reference, with no
    # rebuilds. Picked from any verified variant of the Node.
    reference_variant = Column(
        String(64), default="hero", server_default="hero", nullable=False,
    )

    # node-role §1 — SOURCE OF TRUTH for who may place a node.
    #   'user'   → appears in the WorkflowBuilder palette + the wizard composes
    #              from it. A real user-composable node.
    #   'system' → hidden from the palette + wizard, but the ORCHESTRATOR can
    #              still insert and run it (auto-converters, benchmarks, LLM
    #              inference, trivy scans, etc.). Dogfooding intact: visibility
    #              is not usability. A node flips between roles with one update —
    #              no schema surgery, no moving it out of node_definitions.
    # The column default is 'user' so a node created directly (bypassing the
    # create endpoint) stays visible — the CONSERVATIVE 'system' default is
    # applied deliberately for dev-SEEDED / system-shared nodes in
    # crud.create_node_definition (tenant_id is None → 'system'). Explicit
    # node_role always wins.
    node_role = Column(String(16), nullable=False,
                       default="user", server_default="user", index=True)

    # DERIVED alias of node_role, kept in sync by the `@validates` below so the
    # 20+ existing `system_only`-based filters/guards keep working unchanged.
    # node_role is authoritative; set node_role and this follows.
    system_only = Column(Boolean, default=False, nullable=False)

    @validates("node_role")
    def _sync_system_only_from_role(self, key, value):
        # Fires whenever node_role is SET explicitly (constructor kwarg, crud,
        # the /role endpoint). NOT fired for column defaults, so a raw
        # `NodeDefinition(...)` with neither field set keeps both defaults
        # ('user' / False) consistent without being forced to system.
        role = (value or "user")
        self.system_only = (str(role).strip().lower() == "system")
        return role

    container_timeout = Column(Integer, default=1800, nullable=False)

    # Layer-4 / manifest sidecar — pointer to the signed node manifest
    # written to MinIO by imagebuilder at end of a successful build. The
    # image_digest is the sha256 digest of the pushed image; lets us
    # detect tag-rewrite drift between approval and dispatch.
    manifest_ref = Column(String(500), nullable=True)  # e.g. tenant-42-files/node-manifests/my-node-abc123.json
    image_digest = Column(String(100), nullable=True)  # sha256:abc...

    # FX.73 p4c (owner decision Q56=A) — SOURCE OF TRUTH for "this node is
    # included in the CE (open-source) edition". NULL = not stamped yet.
    # Stamped TRUE on CREATE by services/node_scanner.py for a node whose
    # name is in the CE node-seed keep-list (open-core/node-seed-keeplist.yaml)
    # — the only place a CE install decides what it ships. Read by the palette
    # API (schemas.NodeDefinition.ce_included) for the WorkflowBuilder CE badge.
    # deferred() + nullable keeps reads safe on a DB where migration
    # 20261007zu_fx73_p4c_ce_included.py has not been applied yet.
    ce_included = deferred(Column(Boolean, nullable=True, server_default=text("false")))

    # FX.103 (owner decision Q89=A) — deprecation/retirement lifecycle.
    # 'active' | 'deprecated' | 'retired' (default 'active' = pre-FX.103
    # behaviour: no badge, no run gate). A deprecated node stays runnable for
    # PINNED workflows until retire_after (>= deprecated_at +
    # deprecation.min_runnable_months, enforced by
    # services/node_deprecation.validate_deprecation); the palette API returns
    # the status so the UI can badge it. ISO 27001: status flips are audited
    # (AuditEvent NODE_DEPRECATION_*); nothing here deletes a row.
    status = Column(String(16), nullable=False, default="active", server_default="active", index=True)
    deprecated_at = Column(DateTime(timezone=True), nullable=True)
    retire_after = Column(DateTime(timezone=True), nullable=True)
    # FX.103 part 2 (owner decision Q89=A) — the daily retirement scheduler
    # (services/node_retirement.run_retirement_sweep) sends the 6-month
    # deprecation notice email ONCE per node; this stamp (set when the notice
    # is fully delivered, or when there were no affected tenant admins) stops
    # it re-sending on every subsequent tick. NULL = not sent yet.
    deprecation_notice_sent_at = Column(DateTime(timezone=True), nullable=True)

    # Albedo-pattern 4th artifact — the rendered <Name>_<Type>_PROCESS_Node.py
    # METADATA BLOCK produced server-side from the wizard's metadata dict
    # (via utils.metadata_template.render). Stored here so the wizard can
    # re-render its edit form and the workflow canvas can display the
    # canonical node definition without re-deriving from scattered
    # columns. Optional — legacy system nodes + image-mode nodes don't
    # have this.
    metadata_file_source = Column(Text, nullable=True)

    # Opt-in gVisor OCI runtime per node. Worker threads this into the
    # TaskPayload.UseSandboxRuntime flag at dispatch; executor silently
    # falls back to default runtime if runsc isn't registered on the host.
    use_sandbox_runtime = Column(Boolean, default=False, nullable=False, server_default="false")

    # Marketplace M1 — when TRUE on a system-shared node (tenant_id IS
    # NULL), the node appears in the tenant Marketplace surface but
    # only shows in a tenant's WorkflowBuilder palette after that
    # tenant has clicked "Download" (entitlement row inserted into
    # tenant_node_entitlements). Default FALSE keeps every existing
    # system node auto-included in tenant palettes — no behaviour
    # change for non-marketplace rows. Toggling FALSE on a published
    # node hides it from BOTH the marketplace and every tenant's
    # palette in one switch (entitlement rows remain but the
    # visibility filter requires is_in_marketplace=TRUE to surface).
    is_in_marketplace = Column(Boolean, default=False, nullable=False, server_default="false", index=True)
    # node-ownership-purchases — one-time ownership price for a maintained
    # (system, tenant_id IS NULL) marketplace node. NULL / 0 = free (grant on
    # download as today). Only meaningful on system+marketplace nodes; the
    # purchase endpoint enforces that a priced node is tenant_id IS NULL +
    # is_in_marketplace. Distinct from cost_in_credits (per-run compute).
    price_cents = Column(Integer, nullable=True)
    # Display-only tier band (e.g. 'basic'|'pro'|'enterprise') for store grouping/
    # badges. NOT an authorization input — ownership is what gates use.
    price_tier = Column(String(20), nullable=True)
    # node-trial-runs — the curated demo for try-before-buy. sample_input_ref is a
    # storage pointer to a canned input the auto-generated demo workflow runs on;
    # sample_output_ref is an optional pre-rendered result for the static
    # before/after; demo_workflow_ref overrides the auto graph with a curated one.
    sample_input_ref = Column(String(500), nullable=True)
    sample_output_ref = Column(String(500), nullable=True)
    demo_workflow_ref = Column(String(500), nullable=True)
    # Tenant-internal marketplace review flag. A creator-private node
    # (user_id set, is_in_marketplace False) flips this True when its builder
    # submits it for tenant-admin review. The admin's "accept & publish"
    # clears it and promotes the node to tenant-shared (user_id NULL) +
    # is_in_marketplace True. Drives the draft -> submitted -> published flow.
    review_requested = Column(Boolean, default=False, nullable=False, server_default="false", index=True)

    # P9b — per-variant port/build failure surface. When ImageBuilder's
    # HIP port compiles a broken extension, we'd rather grey that one
    # variant out with a clear reason than fail the whole task. Schema:
    # {variant: reason_string}. Wizard's Review step + Dashboard Tenant
    # Registry tab render these. Scheduler's variants_guard excludes
    # any variant with a non-null entry here — workers never claim a
    # task for a variant we know is broken.
    incompatible_reasons = Column(JSONB, nullable=True)

    # P9e — per-variant smoke-run verification status. Shape:
    # {variant: "pending" | "verified" | "failed"}. A variant starts
    # "pending" the moment ImageBuilder pushes it; the variant_smoke
    # task type runs a forward pass on a worker of that hardware_tag
    # and flips it to "verified" or "failed". Scheduler's variants_guard
    # gates first-dispatch on "verified" OR NULL (never smoke-run →
    # try it once optimistically).
    variant_verification_status = Column(JSONB, nullable=True)

    # P9c — variants that build and run but are flagged as slow on this
    # repo (e.g. NPU on a Gaussian splatting repo falls back to CPU for
    # the custom kernel, ending up 10-50× slower than CUDA). Shape:
    # {variant: reason}. Scheduler's variants_guard soft-defers these:
    # prefers other compatible variants, only routes to not_recommended
    # when no other hardware_tag matches the worker pool.
    not_recommended_variants = Column(JSONB, nullable=True)

    # P9l — per-variant throughput benchmark vs CUDA baseline, populated
    # by the smoke-worker's variant_bench.py run. Shape:
    # {variant: {throughput_tps: float, p50_latency_ms: float,
    # ref_cuda_ratio: float}}. Wizard tiles display the ratio next to
    # each variant; scheduler prefers higher-ratio variants on ties.
    variant_performance = Column(JSONB, nullable=True)

    # FX.91 p2a (owner Q77=A) — the node manifest's declared external
    # hostnames this node needs, e.g. ["huggingface.co", "*.cdn.jsdelivr.net"].
    # Plain hostnames only (no scheme/port/path/IPs); a leading `*.` label is
    # the only wildcard. NULL = the manifest declared nothing = default-deny,
    # which is also what a pre-migration row reads as, so this stays deferred
    # (models.py is bind-mounted live and can outlive
    # migrations/20261007zx_fx91_p2a_node_egress_domains.py).
    # Read through services/node_egress_domains.py::get_node_egress_domains;
    # distinct from `allowed_egress` ({host,ports,protocol} + IP/CIDR), which
    # stays the dispatch-time compile input. Neither replaces the other.
    egress_domains = deferred(Column(JSONB, nullable=True))

    __table_args__ = (
        # Wizard-rebuild §11b — uniqueness excludes soft-deleted rows so
        # a user who deletes "MyNode" can immediately recreate one with
        # the same name (the tombstone hangs around for the tenant's
        # retention period but doesn't squat the name).
        Index(
            "uq_node_def_live_tenant_user_name",
            "tenant_id", "user_id", "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        # System-node uniqueness (tenant_id IS NULL) — same partial-on-
        # deleted_at predicate. sqlite_where clone keeps the test DB
        # semantically matched to prod for the wizard regression suite.
        Index(
            "uq_system_live_node_def_name",
            "name",
            unique=True,
            postgresql_where=text("tenant_id IS NULL AND deleted_at IS NULL"),
            sqlite_where=text("tenant_id IS NULL AND deleted_at IS NULL"),
        ),
        # Tiered pricing (migrations/20260513_tiered_credit_pricing.py): a
        # non-empty tier map must agree with cost_in_credits on 'medium'.
        CheckConstraint(
            "cost_credits_by_tier = '{}'::jsonb OR cost_in_credits = (cost_credits_by_tier->>'medium')::int",
            name="ck_node_def_cost_in_credits_matches_medium",
        ),
    )


class NodeEgressApproval(SoftDeleteWithActorMixin, Base):
    """FX.91 p2c [B] — a tenant admin's ONE-TIME approval of a node's
    declared egress hostnames (GSEC Q77=A contract, egress default-deny).

    One row per (tenant, node) — the node's `egress_domains` (FX.91 p2a,
    read through services/node_egress_domains.get_node_egress_domains) is
    what the admin looked at, so the row stores the EXACT canonical domain
    set that was approved (``domains_approved``). Approval is therefore
    only valid while the node still declares that same set: a re-scan or
    PATCH that changes the declaration (added/removed/wildcard-widened
    host) makes the row stale and the gate re-asks — no silent widening.

    Lifecycle (ISO 27001 — NO hard delete, ever):
      * approve  -> row (deleted_at NULL); re-approving the same node
        UPDATES the live row's set + stamps (the audit trail keeps every
        decision, append-only).
      * revoke   -> soft revoke: stamps ``deleted_at``/``deleted_by_user_id``
        (the mixin) + ``revoked_at``/``revoked_by_user_id`` so the reason
        the gate now refuses is visible; a later approval REVIVES the row
        (clears the tombstone) instead of orphaning a second row.

    ``tenant_id`` NULL = a platform/system node (system nodes carry
    tenant_id NULL on node_definitions): its approval is install-wide and
    the tenant admin who reviewed it is recorded in ``approved_by_user_id``.
    """
    __tablename__ = "node_egress_approvals"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), index=True, nullable=True)
    node_definition_id = Column(
        ForeignKey("node_definitions.id"), index=True, nullable=False,
    )
    approved_by_user_id = Column(
        ForeignKey("users.id"), index=True, nullable=True,
    )
    approved_at = Column(DateTime(timezone=True), nullable=True)
    domains_approved = Column(JSONB, nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    __table_args__ = (
        # One live approval row per (tenant, node) — the tombstone (revoked
        # or system-scope NULL tenant) never squats the key, so a node can
        # be revoked and re-approved without a second row. Same partial-
        # index idiom the node_definitions name uniqueness uses.
        Index(
            "uq_node_egress_approval_live_tenant_node",
            "tenant_id", "node_definition_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )


from sqlalchemy import event as _sa_event
from sqlalchemy import inspect as _sa_inspect


def _node_def_sync_tiers(connection, target, *, inserting):
    # Pre-migration live DBs lack the tier columns: touching the deferred
    # attributes there would emit SQL naming a missing column, so do nothing.
    from utils.node_cost import sync_tiers_to_cost, tier_columns_available

    if not tier_columns_available(connection):
        return
    state = _sa_inspect(target)
    if inserting:
        if "cost_credits_by_tier" not in state.dict:
            return  # server default '{}' always satisfies the CHECK
    else:
        attrs = state.attrs
        if not (attrs.cost_in_credits.history.has_changes()
                or attrs.cost_credits_by_tier.history.has_changes()):
            return  # persisted row already satisfies the CHECK
    sync_tiers_to_cost(target)


@_sa_event.listens_for(NodeDefinition, "before_insert")
def _node_def_before_insert(mapper, connection, target):
    _node_def_sync_tiers(connection, target, inserting=True)


@_sa_event.listens_for(NodeDefinition, "before_update")
def _node_def_before_update(mapper, connection, target):
    _node_def_sync_tiers(connection, target, inserting=False)


class NamedArtifact(Base):
    """Per-tenant named registry of reusable pipeline artifacts (named-artifact-registry).

    A small, shared handle table so workflow nodes can refer to a produced
    artifact BY NAME instead of by an opaque collection id / filename the user
    has to copy around:

      * type == "rag"  — a built RAG corpus. ``ref`` is the vector-store
        collection / namespace the RAG Query node reads back. ``metadata`` holds
        doc/chunk counts.
      * type == "lora" — a trained SDXL character LoRA. ``ref`` is the
        RESOLVABLE location of the ``.safetensors`` (the filename the brand-sdxl
        server finds under its mounted ``/loras`` dir, i.e. resolve-by-name for
        the Generate node). ``metadata`` holds base model / trigger word / steps.

    Scoped per tenant. Registered/updated (upsert) by the producing node's
    run token and listed by the consuming node (and the WB dropdown) via the
    platform ``/named-artifacts`` router. Uniqueness is per
    (tenant_id, type, name) among LIVE rows — soft-deleted tombstones don't
    squat the name (ISO 27001: soft-delete only, never hard delete).
    """
    __tablename__ = "named_artifacts"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer, ForeignKey("tenants.id"), nullable=False, index=True,
    )
    # "rag" | "lora" — kept a plain string (not an Enum) so adding a future
    # artifact type is additive and needs no enum migration.
    type = Column(String(16), nullable=False)
    name = Column(String(200), nullable=False)
    # rag: collection / namespace; lora: resolvable location (filename under the
    # SDXL server's /loras, or an S3 key + fetch step).
    ref = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    # rag: {documents, chunks, upserted, embedding_model}; lora: {base_model,
    # trigger_word, steps, ...}. Mapped attribute is `artifact_metadata` because
    # `metadata` is reserved on the declarative Base; the DB column is `metadata`.
    artifact_metadata = Column("metadata", JSONB, nullable=True)
    # FX.131 (Q118=A) — training provenance record, additive/nullable. On a
    # trained model (type == "lora") records the dataset ids it was trained on
    # and, when the character's likeness consent (Q84) was captured, a link to
    # that consent record. Shape: {"dataset_ids": [...], "consent_record_id": N,
    # "consent_type": "biometric"} — keys optional, values never secrets.
    # Nullable so pre-existing rows and rag-type artifacts stay untouched.
    training_record = Column(JSONB, nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

    tenant = relationship("Tenant")

    __table_args__ = (
        # Per-tenant unique on (type, name) among live rows only, so a
        # soft-deleted artifact doesn't block re-registering the same name.
        Index(
            "uq_named_artifact_live_tenant_type_name",
            "tenant_id", "type", "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )


class NodeVersion(Base):
    """node-version-pinning — one immutable row per node build.

    Today `NodeDefinition.version` is a mutable integer and every rebuild moves
    `:latest` in place, so the only durable handle on a prior build is its image
    digest sitting orphaned in the registry. This table makes each build
    addressable: a workflow pins a `node_versions.id`, a run records the digest
    it executed, and a later rebuild adds a NEW row rather than overwriting the
    old one. The digest — not the tag — is the execution reference.

    Rows are append-only: never mutate `image_digest`/`gpu_variant_digests` on an
    existing row, or the pin it backs stops being reproducible.
    """

    __tablename__ = "node_versions"
    id = Column(Integer, primary_key=True)
    node_definition_id = Column(
        ForeignKey("node_definitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalized so the enqueue path can resolve a pin by node_kind without a
    # join, mirroring how dispatch already keys on node_kind.
    node_kind = Column(String(250), nullable=True, index=True)
    # A human-friendly monotonic counter (v1, v2, v3…) ASSIGNED when the row is
    # recorded — NOT NodeDefinition.version, which the build path never bumps.
    # The image_digest below is the true identity; version is for display/order.
    version = Column(Integer, nullable=False)
    # sha256:... of the default/hero image for this build — the immutable handle
    # and the row's identity (unique per node definition, see __table_args__).
    image_digest = Column(String(100), nullable=False)
    # Snapshot of the per-variant image map at build time (backend -> image ref),
    # so a pinned run resolves variants from the version, not the live node.
    gpu_variants = Column(JSONB, nullable=True)
    # Per-variant immutable digests (backend -> sha256:...), parallel to
    # gpu_variants. Populated by the build pipeline; a missing entry means that
    # variant is pulled by tag (the worker degrades gracefully).
    gpu_variant_digests = Column(JSONB, nullable=True)
    # Default image ref snapshot (NodeDefinition.executable_path at build).
    executable_path = Column(String(255), nullable=True)
    # Pointer to the signed manifest for this build, mirroring NodeDefinition.manifest_ref.
    manifest_ref = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    node_definition = relationship("NodeDefinition")

    __table_args__ = (
        # Digest is identity: one row per distinct build of a node. A rebuild
        # with a new digest is a new version; re-recording the same digest is a
        # no-op. version is a display counter, not the key.
        UniqueConstraint("node_definition_id", "image_digest", name="uq_node_version_digest"),
    )


class WorkflowNode(Base):
    __tablename__ = "workflow_nodes"
    id = Column(Integer, primary_key=True)
    workflow_id = Column(ForeignKey("workflows.id"), index=True)
    node_definition_id = Column(ForeignKey("node_definitions.id"))
    graph_node_id = Column(String(255))
    configuration = Column(JSONB)
    # node-version-pinning — the immutable node build this workflow node runs.
    # NULL means "resolve the current build at dispatch" (legacy behaviour, and
    # the state of every row until backfill/save pins it). When set, the enqueue
    # path resolves the image digest from this NodeVersion so a later rebuild
    # does not change what this workflow executes; upgrading is an explicit
    # re-pin to a newer node_versions.id.
    pinned_node_version_id = Column(
        ForeignKey("node_versions.id"), nullable=True, index=True
    )
    # Relationships
    workflow = relationship("Workflow", back_populates="nodes")
    node_definition = relationship("NodeDefinition")
    pinned_node_version = relationship("NodeVersion")

    @property
    def node_kind(self):
        """Get node_kind from associated node_definition"""
        return self.node_definition.node_kind if self.node_definition else None

    @property
    def cost_in_credits(self):
        """Get cost_in_credits from associated node_definition"""
        return self.node_definition.cost_in_credits if self.node_definition else 0


class BuildSpecRecord(SoftDeleteWithActorMixin, Base):
    """δ DAG-wizard B4a — persisted serialized BuildSpec.

    Stored when the wizard's /build-and-register-node endpoint
    accepts a build, before the workflow run kicks off. Each
    per-task DAG container (build_one_variant, smoke_one_variant,
    etc.) reads the full spec by id when it runs — keeps the
    graph_json's node data lean (just a build_spec_id reference)
    while the heavy artifact bytes (Dockerfiles, sample input,
    multi-file source bundles) stay in this row's spec_json
    column.

    Lifecycle:
    - INSERTed by the dispatch endpoint
    - workflow_run_id wired up after orchestrator creates the run
    - SELECTed by per-task containers via build_spec_id env var
    - GC'd by the retention_cleanup APScheduler job alongside
      old workflow_run logs

    Per-tenant ON DELETE CASCADE drops orphaned specs when a
    tenant offboards. workflow_run_id ON DELETE SET NULL keeps
    the spec around even if the run gets pruned (helps with
    debug-time replay).
    """
    __tablename__ = "build_specs"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    workflow_run_id = Column(ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    spec_json = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class NodeSourceRevision(SoftDeleteWithActorMixin, Base):
    """node-code-edit-rebuild §1 — durable, ordered source revisions per node.

    The auto-generated (revision 1) or hand-edited/copied source for a node is
    stored here as an ordered chain and is the DURABLE source of truth —
    ``build_specs.spec_json`` is GC'd by the retention sweep, so it cannot hold a
    registered node's code long-term. ``GET /node_definitions/{id}/source`` reads
    the latest non-deleted revision.

    RETENTION: ``deleted_at`` marks an explicit, audited user deletion; those
    tombstones are pruned after 90 days — see ``services.node_source_revisions.prune_expired``,
    wired into the ``hard_delete_after_retention`` sweep. Active (non-deleted)
    revisions are never reaped by age.

    The node FK is ON DELETE SET NULL (with a denormalized ``node_name``) so a
    revision survives even if its node is later retired.
    """
    __tablename__ = "node_source_revision"
    id = Column(Integer, primary_key=True)
    node_definition_id = Column(
        ForeignKey("node_definitions.id", ondelete="SET NULL"), nullable=True, index=True)
    node_name = Column(String(100), nullable=True)
    # Nullable — mirrors NodeDefinition.tenant_id: a SYSTEM node (tenant_id NULL,
    # MOD-authored) has tenant-less source revisions. A tenant's own / forked
    # node carries its tenant_id (CASCADE on tenant offboard).
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    revision_no = Column(Integer, nullable=False)
    parent_revision_id = Column(
        ForeignKey("node_source_revision.id", ondelete="SET NULL"), nullable=True)
    origin = Column(String(20), nullable=False, default="edited")  # generated|copied|edited|reverted
    author_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # {dockerfile, entrypoint_sh, build_and_push_sh, source_files: [{filename, content}]}
    hero = Column(JSONB, nullable=False, default=dict, server_default='{}')
    source_overrides_by_variant = Column(JSONB, nullable=False, default=dict, server_default='{}')
    variants_snapshot = Column(JSONB, nullable=True)
    critic_warnings = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class NodeVariantVerification(SoftDeleteWithActorMixin, Base):
    """Wizard-rebuild §5 — per-variant sample-test verification row.

    One row per (Node, variant) pair. Identifies the built variant by
    either ``build_spec_id`` (during the workflow run, before
    register completes) or ``node_definition_id`` (after register).
    The register step backfills ``node_definition_id`` on existing
    build-time rows.

    Columns:
      - ``score``: similarity in [0, 1]. For hero variants this is
        1.0 when the sample-test container exited 0 with output
        (hero IS the reference); for non-hero it's the similarity
        score vs ``scored_against_variant``.
      - ``verdict``: ``verified`` / ``drift`` / ``marginal`` /
        ``failed`` / ``incompatible`` / ``untested`` (see
        `publish_thresholds.verdict_for_score`).
      - ``scored_against_variant``: which variant was the reference
        at scoring time. Preserved so a later reference-variant
        change can detect stale rows and recompute.
      - ``raw_output_ref``: MinIO object key for the produced
        output bytes, kept so the §5c PATCH-reference-variant
        endpoint can re-score in place without rebuilding.
    """
    __tablename__ = "node_variant_verification"
    id = Column(Integer, primary_key=True)
    build_spec_id = Column(
        ForeignKey("build_specs.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    node_definition_id = Column(
        ForeignKey("node_definitions.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    variant = Column(String(64), nullable=False)
    score = Column(Float, nullable=True)
    verdict = Column(String(32), nullable=False)
    scored_against_variant = Column(String(64), nullable=True)
    scored_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    output_type_at_test = Column(String(32), nullable=True)
    raw_output_ref = Column(String(512), nullable=True)
    # project-agent §1.3 — the row describes the BEST attempt, not the
    # latest. score / verdict / raw_output_ref / image_digest all move
    # together and only when a new attempt beats the incumbent, so a
    # repair that made things worse cannot silently replace a good build.
    # attempt_history keeps every attempt including the regressions.
    attempt_count = Column(Integer, nullable=False, default=1, server_default="1")
    best_score = Column(Float, nullable=True)
    # Pushed manifest digest for the published attempt. Recorded because
    # moving :latest does not delete the earlier manifest — ImageBuilder
    # already pulls-by-digest-and-retags on its source-hash cache path,
    # so a regression is revertible without a rebuild.
    image_digest = Column(String(255), nullable=True)
    attempt_history = Column(JSONB, nullable=False, default=list,
                             server_default="[]")
    # project-agent §1.4 — a non-hero variant scoring below DRIFT_THRESHOLD
    # used to publish anyway with a warning chip, on the assumption a human
    # would see the chip. Selection is by placement and never looks at a
    # chip, so nothing stopped user work routing to the divergent build.
    # NULL = awaiting a ruling (and §1.5 will not select it). Otherwise
    # 'publish' / 'drop' / 'keep_fixing'. Seeded from the tenant's
    # node_build.drift_default setting when one is configured.
    drift_ruling = Column(String(16), nullable=True)
    drift_ruled_at = Column(DateTime(timezone=True), nullable=True)
    drift_ruled_by_user_id = Column(ForeignKey("users.id"), nullable=True)


class VariantTestPending(Base):
    """Wizard-rebuild §9 — speculative variant builds queue.

    The build pipeline produces images for every variant the source
    can target, regardless of whether matching accelerator hardware
    is online RIGHT NOW. Sample-tests that need hardware that isn't
    present queue here; the worker-heartbeat hook drains matching
    rows when a worker with the right accelerator joins.

    State machine:
      pending → claimed (worker heartbeat picks it up)
      pending → expired (TTL hit, no matching hardware appeared)
      claimed → verified | failed (sample-test result lands)

    30-day default TTL — generous because the wizard's promise is
    "build for any future hardware". Expired rows flip the Node's
    variant to ``untested_awaiting_hardware`` so the scheduler
    refuses to route real workloads to it.
    """
    __tablename__ = "variant_test_pending"
    id = Column(Integer, primary_key=True)
    build_spec_id = Column(
        ForeignKey("build_specs.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    node_definition_id = Column(
        ForeignKey("node_definitions.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    variant = Column(String(64), nullable=False)
    image_ref = Column(String(512), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    claimed_by_worker_id = Column(
        ForeignKey("worker_licenses.id", ondelete="SET NULL"),
        nullable=True,
    )
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(16), default="pending", nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)


class RepoDocsExtract(SoftDeleteWithActorMixin, Base):
    """Wizard-rebuild §3c — cached repo-docs extraction.

    One row per (tenant, repo_url, ref, extractor_version). Hit by
    the Git-mode BuildPlan extractor to avoid re-fetching +
    re-chunking + re-tagging on every wizard submission for the
    same repo.

    7-day TTL; APScheduler hourly sweep
    (`_run_repo_docs_extract_sweep`) deletes rows where
    `expires_at < NOW()`. When BuildPlan.EXTRACTOR_VERSION bumps,
    old rows coexist with new ones until the sweep purges them —
    so a version bump never invalidates the cache mid-flight.

    `chunks_json` stores a list of DocChunk dicts with intents
    already populated. `files_missing` is a list of sentinel file
    paths the fetcher tried but didn't find — surfaces in the View
    Plan UI so users know we looked.
    """
    __tablename__ = "repo_docs_extract"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    repo_url = Column(Text, nullable=False, index=True)
    ref = Column(Text, nullable=False)
    extractor_version = Column(Integer, nullable=False)
    chunks_json = Column(JSONB, nullable=False)
    files_missing = Column(JSONB, nullable=False, default=list)
    fetched_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    expires_at = Column(
        DateTime(timezone=True), nullable=False, index=True,
    )

    __table_args__ = (
        Index(
            "uq_repo_docs_extract_live_key",
            "tenant_id", "repo_url", "ref", "extractor_version",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )


class CalibrationPlan(Base):
    """Calibration coordinator — replaces the fire-and-forget batch
    dispatch (`run_batch_calibration` enqueues all 42k tasks at once)
    with a stateful coordinator that drips work into the queue at a
    bounded rate.

    One row = one in-flight plan per tenant (partial UNIQUE index on
    `status='running'` enforces single-active). The APScheduler
    `calibration_coordinator.tick` job reads this row, counts in-flight
    calibration tasks (CLAIMED + RUNNING + ASSIGNED), and enqueues the
    next NodeDef's 84-task matrix only when in-flight is below
    `target_in_flight`.

    Lifecycle:
    - `running` → `paused` (manual via PATCH) or `completed` (sweep
      detected nothing left to dispatch and in-flight reached 0) or
      `cancelled` (explicit DELETE) or `halted` (automatic — the
      failure-rate brake in services/calibration_coordinator.py saw
      recent calibration tasks failing at or above the configured
      threshold and stopped dispatch).
    - `halted` is distinct from `paused` on purpose: `paused` means an
      operator chose to stop, `halted` means the platform stopped
      itself. Conflating them makes "why did this stop?" a log-archaeology
      exercise. Resuming a halted plan is an operator action.
    - On `cancelled`, all PENDING calibration tasks tagged to this
      plan are also moved to CANCELLED so they don't keep draining, and
      any node left in `calibration_status='calibrating'` is released
      back to `uncalibrated` so it isn't stranded out of future plans.
    """
    __tablename__ = "calibration_plans"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="running")
    target_in_flight = Column(Integer, nullable=False, default=30)
    total_nodes = Column(Integer, nullable=False)
    nodes_completed = Column(Integer, nullable=False, default=0)
    nodes_failed = Column(Integer, nullable=False, default=0)
    nodes_in_progress = Column(JSONB, nullable=False, default=list)
    last_tick_at = Column(DateTime(timezone=True), nullable=True)
    last_tick_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class TenantNodeEntitlement(SoftDeleteMixin, Base):
    """Marketplace M1 — record of a tenant having downloaded
    (entitled itself to) a marketplace-published NodeDefinition.

    The entitlement is the tenant's opt-in to see a marketplace node
    in its WorkflowBuilder palette. Without an entitlement row, a
    marketplace-flagged system node is invisible to the tenant.
    With one, it shows up alongside the tenant's own private + tenant-
    shared nodes via the existing visibility filter in
    crud.get_node_definitions.

    Entitlement-based (not row-clone): a single platform-admin
    marketplace toggle cascades. When MOD flips
    NodeDefinition.is_in_marketplace from TRUE to FALSE, the
    entitlement rows remain in this table but the palette filter
    short-circuits (is_in_marketplace must be TRUE for rows to
    surface). Re-enabling the toggle restores the tenant's view
    without re-asking them to download.

    No payment / pricing fields at v1 — marketplace is free; only MOD
    publishes; tenants opt in for free. Pricing-per-node would be a
    future column on this row.
    """
    __tablename__ = "tenant_node_entitlements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "node_definition_id", name="uq_tenant_node_entitlement"),
    )
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    node_definition_id = Column(ForeignKey("node_definitions.id", ondelete="CASCADE"), nullable=False, index=True)
    granted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    granted_by_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # node-ownership-purchases — how this entitlement was granted, which governs
    # its lifecycle. String (not a PG enum) on purpose: enum migrations are
    # painful in this schema. Allowed values:
    #   'plan_grant'  — included by the tenant's plan; the reconciler may REVOKE
    #                   it when the plan no longer includes the node.
    #   'purchase'    — bought one-time; PERMANENT, never revoked by plan changes.
    #   'admin_grant' — free/legacy download or admin gift; permanent.
    # Backfill sets every pre-existing row to 'admin_grant' (preserves today's
    # free access). Only 'plan_grant' rows are ever touched by the reconciler.
    source = Column(String(20), nullable=False, default="admin_grant", server_default="admin_grant")
    # The purchase that created a 'purchase' row (NULL otherwise) — for receipts
    # and refund-driven revocation.
    purchase_id = Column(ForeignKey("node_purchases.id", ondelete="SET NULL"), nullable=True, index=True)
    # GG.6b — soft revocation marker: when set (and in the past) this
    # entitlement is NOT active. Entitlement checks must apply
    # services.entitlement_source.active_filter() so an expired row reads
    # as "not entitled" without ever hard-deleting the row (ISO 27001).
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)


class NodePurchase(Base):
    """node-ownership-purchases — ledger of one-time node/package ownership buys.

    One row per successful Stripe one-time charge. Distinct from per-run compute
    metering (CreditUsageLog): this is the perpetual, tenant-wide *ownership* fee.
    `stripe_payment_intent_id` is unique so the payment-success webhook is
    idempotent. `refunded_at` set on a refund; the entitlement it backs is then
    revoked only if the node is not still plan-granted.
    """
    __tablename__ = "node_purchases"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    # Exactly one of node_definition_id / package_id is set (package = Phase 2).
    node_definition_id = Column(ForeignKey("node_definitions.id", ondelete="SET NULL"), nullable=True, index=True)
    package_id = Column(Integer, nullable=True, index=True)  # FK added with NodePackage (Phase 2)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default="usd", server_default="usd")
    stripe_payment_intent_id = Column(String(255), nullable=True, unique=True)
    purchased_by_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    refunded_at = Column(DateTime(timezone=True), nullable=True)


class PlanIncludedNode(Base):
    """node-ownership-purchases — a maintained node a plan grants FREE to its
    subscribers (Phase 1 direct plan->node mapping; Phase 2 supersedes this with
    packages). The reconciler creates/removes source='plan_grant' entitlements to
    match a tenant's active plan; buying the node upgrades that row to 'purchase'.
    """
    __tablename__ = "plan_included_nodes"
    __table_args__ = (
        UniqueConstraint("plan_id", "node_definition_id", name="uq_plan_included_node"),
    )
    id = Column(Integer, primary_key=True)
    plan_id = Column(ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True)
    node_definition_id = Column(ForeignKey("node_definitions.id", ondelete="CASCADE"), nullable=False, index=True)


class NodePackage(Base):
    """node-ownership-purchases Phase 2 — a bundle of maintained nodes, sold as
    ONE one-time purchase and/or included in a plan. Official (system) packages
    have tenant_id NULL. price_cents is the a-la-carte bundle price (NULL/0 = not
    sold on its own, only granted via a plan). Buying a package grants 'purchase'
    ownership of every node in it under a single NodePurchase (package_id set).
    """
    __tablename__ = "node_packages"
    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False)
    slug = Column(String(150), nullable=True, index=True)
    description = Column(Text, nullable=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True)  # NULL = official
    price_cents = Column(Integer, nullable=True)
    price_tier = Column(String(20), nullable=True)
    is_in_marketplace = Column(Boolean, default=False, nullable=False, server_default="false", index=True)
    # GG.6c — private listings: 'public' (default)|'private'|'unlisted'.
    # See services/listing_visibility.py for the shared enforcement helper.
    visibility = Column(String(20), nullable=False, default="public", server_default="public")
    allowed_tenant_ids = Column(JSONB, nullable=False, default=list, server_default="[]")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class NodePackageItem(Base):
    """A node's membership in a NodePackage."""
    __tablename__ = "node_package_items"
    __table_args__ = (
        UniqueConstraint("package_id", "node_definition_id", name="uq_node_package_item"),
    )
    id = Column(Integer, primary_key=True)
    package_id = Column(ForeignKey("node_packages.id", ondelete="CASCADE"), nullable=False, index=True)
    node_definition_id = Column(ForeignKey("node_definitions.id", ondelete="CASCADE"), nullable=False, index=True)


class PlanPackage(Base):
    """A package a plan grants FREE to subscribers. The reconciler expands this to
    per-node source='plan_grant' entitlements (alongside PlanIncludedNode)."""
    __tablename__ = "plan_packages"
    __table_args__ = (
        UniqueConstraint("plan_id", "package_id", name="uq_plan_package"),
    )
    id = Column(Integer, primary_key=True)
    plan_id = Column(ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True)
    package_id = Column(ForeignKey("node_packages.id", ondelete="CASCADE"), nullable=False, index=True)


class PackageItem(SoftDeleteMixin, Base):
    """GG.1 (launch-completion-megaplan, gg-commerce) — generalized bundle
    membership. NodePackage (formerly node-only, via NodePackageItem) can now
    also contain stacks and workflows, so a bundle can mix all three kinds.

    `kind` is a plain String + CHECK (not a PG enum) on purpose, matching the
    convention already used for TenantNodeEntitlement.source above — enum
    migrations (ALTER TYPE ADD VALUE) are the single most fragile DDL this
    codebase runs. `ref_id` is a polymorphic reference: node_definitions.id
    when kind='node', a GF stack_listings.id when kind='stack', or
    workflow_listings.id when kind='workflow'. No FK constraint on ref_id
    (the target table differs per kind, and the stack_listings table is a separate
    model (StackListing below)).

    NodePackageItem is NOT replaced or migrated away — it stays the source of
    truth for existing node-only readers (crud.node_marketplace_paid, etc.);
    this table is backfilled from it (kind='node') so new bundle-aware
    readers (GG.2 catalog, GG.3 closure) can query one table across kinds
    without touching the old one. No hard deletes (ISO 27001): rows are
    soft-deleted via `deleted_at`, mirroring NodePackageItem's row shape.
    """
    __tablename__ = "package_items"
    __table_args__ = (
        UniqueConstraint("package_id", "kind", "ref_id", name="uq_package_item_kind_ref"),
        CheckConstraint("kind IN ('node', 'stack', 'workflow')", name="ck_package_item_kind"),
        Index("ix_package_items_kind_ref", "kind", "ref_id"),
    )
    id = Column(Integer, primary_key=True)
    package_id = Column(ForeignKey("node_packages.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(20), nullable=False, default="node", server_default="node")
    ref_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkflowListing(SoftDeleteMixin, Base):
    """GG.1 (gg-commerce) — a published, version-pinned snapshot of a
    workflow offered for sale/claim in the catalog (design choice: "Workflows
    get a new `workflow_listings` table: a published, version-pinned snapshot
    = graph_json + each node's image digest + the required stack listing
    ids").

    One row per published VERSION (not per workflow) — `(source_workflow_id,
    version)` is unique, mirroring the node pinning pattern used elsewhere
    (executed_image_digest / pinToDigest). `snapshot_json` is the graph at
    publish time (same `{nodes:[...], edges:[...]}` shape as
    WorkflowVersion.graph_json / WorkflowTemplate.graph_json); it is what
    GG.6 `install_workflow` clones and what GG.8 sealed-mode expands
    server-side. `node_digests` pins each node's image digest at publish
    time so a buyer's install always runs what the publisher tested, even if
    the publisher's own node images move later (same "Update to vN" model as
    node pinning). `required_stack_listing_ids` is a plain JSONB id list
    (not an FK array) because GF's `stack_listings` table does not exist in
    this codebase yet; the closure resolver (optional seam) reads it; CE answers
    the no-gate closure.

    `licence` is 'open' (buyer gets an editable copy) or 'sealed' (run-only
    app; GG.8) — owner-decided 2026-09-27: "free = open, paid = sealed by
    default, publisher can choose open." Default 'open' here; GG.5/GG.6
    checkout flow sets 'sealed' for restricted listings unless the publisher opts
    out. `status` gates catalog visibility (draft/published/unlisted/
    archived); only 'published' rows are catalog-visible (GG.2).
    """
    __tablename__ = "workflow_listings"
    __table_args__ = (
        UniqueConstraint("source_workflow_id", "version", name="uq_workflow_listing_source_version"),
        Index("ix_workflow_listings_status", "status"),
    )
    id = Column(Integer, primary_key=True)
    publisher_tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    # SET NULL (not CASCADE): the snapshot is self-contained (snapshot_json),
    # so a listing must survive the source workflow being deleted later.
    source_workflow_id = Column(ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True, index=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    snapshot_json = Column(JSONB, nullable=False)
    node_digests = Column(JSONB, nullable=True)
    required_stack_listing_ids = Column(JSONB, nullable=True)
    price_cents = Column(Integer, nullable=True)
    price_tier = Column(String(20), nullable=True)
    licence = Column(String(20), nullable=False, default="open", server_default="open")
    status = Column(String(20), nullable=False, default="draft", server_default="draft")
    # GG.6c — private listings: 'public' (default)|'private'|'unlisted'.
    # See services/listing_visibility.py for the shared enforcement helper.
    visibility = Column(String(20), nullable=False, default="public", server_default="public")
    allowed_tenant_ids = Column(JSONB, nullable=False, default=list, server_default="[]")
    # GSEC FX.95 p1 (owner decision Q81=A) — marketplace listing review
    # state machine (services/listing_review.py): submitted ->
    # auto_checks -> staff_review -> live|rejected. NULL = legacy row
    # published before the review gate existed; the migration backfills
    # 'live' for every already-published row so storefront behaviour is
    # unchanged. `status` stays the catalog-visibility gate (only
    # 'published' rows are catalog-visible) — `approve()` flips review_status
    # to 'live' AND sets status='published', so this column never changes
    # storefront filtering on its own.
    review_status = Column(String(20), nullable=True)
    reviewed_by = Column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    rejection_reason = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    # GSEC FX.95 p2 — auto-check pipeline results (malware/vuln/
    # licence/egress-sandbox; services/listing_review_checks.py).
    # [{name, status: pass|fail|warn|skipped, detail}, ...] at the time
    # of the last pipeline run. A fail does NOT reject — the listing
    # stays in the staff queue flagged.
    check_results = Column(JSONB, nullable=True)
    checks_ran_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class TenantWorkflowEntitlement(SoftDeleteMixin, Base):
    """GG.1 (gg-commerce) — record of a tenant owning a published
    WorkflowListing, mirroring TenantNodeEntitlement (L2650) exactly so the
    future entitlement_guard (GG.4) and catalog (GG.2) can treat nodes and
    workflows the same way.

    `installed_workflow_id` points at the clone GG.6 `install_workflow`
    creates in the buyer's tenant (`source_listing_id`/`source_version` live
    on that Workflow row, added when GG.6 builds the clone path — out of
    scope for this additive-schema task). It is nullable because the
    entitlement can exist before the clone is materialized (e.g. a sealed
    listing that expands server-side per-run rather than being cloned).

    `source` reuses TenantNodeEntitlement's convention (purchase/plan_grant/
    admin_grant default) rather than the full purchase|free|plan|grant|
    invite|trial vocabulary GG.6b designs — that column stays additive
    (String, not enum) so GG.6b can widen the allowed values later without a
    migration. No `purchase_id` FK yet: there is no workflow-purchase ledger
    table in this codebase (GG.5 adds checkout); adding one now would be
    speculative given GG.1's brief is schema-only, not checkout.
    """
    __tablename__ = "tenant_workflow_entitlements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "workflow_listing_id", name="uq_tenant_workflow_entitlement"),
    )
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    workflow_listing_id = Column(ForeignKey("workflow_listings.id", ondelete="CASCADE"), nullable=False, index=True)
    installed_workflow_id = Column(ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True, index=True)
    granted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    granted_by_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    source = Column(String(20), nullable=False, default="admin_grant", server_default="admin_grant")
    # GG.6b — soft revocation marker, same convention as
    # TenantNodeEntitlement.expires_at: set (and past) => not entitled.
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)


class WorkflowRunEvent(Base):
    """#12 (launch-day-followup-2) — orchestrator/traffic-cop events
    surfaced in the run-detail Logs panel. Container output already
    appears there; this table closes the gap between two running
    containers ("dispatched task X at T+150ms", "no eligible worker
    for queue Y", "downstream node Z queued"). Same retention as
    workflow_runs — rows fall out alongside their parent run.
    """
    __tablename__ = "workflow_run_events"
    id = Column(BigInteger, primary_key=True)
    workflow_run_id = Column(Integer, ForeignKey("workflow_runs.id"), nullable=False, index=True)
    ts = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    source = Column(String(32), nullable=False)   # 'orchestrator' | 'traffic_cop' | 'worker'
    level = Column(String(16), nullable=False, default='info')  # 'info' | 'warn' | 'error'
    message = Column(Text, nullable=False)
    event_metadata = Column(JSONB, nullable=True)


class NodeRun(Base):
    __tablename__ = "node_runs"
    id = Column(Integer, primary_key=True)
    workflow_run_id = Column(ForeignKey("workflow_runs.id"), index=True)
    workflow_node_id = Column(ForeignKey("workflow_nodes.id"), index=True)
    status = Column(Enum(TaskStatus), default=TaskStatus.QUEUED, index=True)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    inputs = Column(JSONB)
    outputs = Column(JSONB)
    # Model-call token metering (GX c11, 30.6.11): written ONLY for the node that
    # runs a platform stack model -- `{"models":[{node_id, model, prompt_tokens,
    # completion_tokens, total_tokens, images}]}`. NULL means "not a model call",
    # which is why the column is nullable with no default of any kind.
    usage_tokens = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    # AI scheduler — per-node ETA stamped at run creation; replaced with actual
    # duration on completion so critical-path recompute shows real-time delta.
    estimated_duration_seconds = Column(Integer, nullable=True)
    # Per-node share of the run's input size (e.g. for a fan-out where each
    # branch processes a subset of files). Falls back to the parent run's
    # input_size_bytes when not explicitly set.
    input_size_bytes = Column(BigInteger, nullable=True)
    # Gate 3 — the durable volume every ATTEMPT of this node run shares.
    #
    # Keyed to the node run rather than the attempt, which is the whole point:
    # the ordinary task volume is ephemeral (`modcore-task-` prefix, removed by
    # the worker's `defer cleanupVolume`), so attempt 2 starts blank and a node
    # five hours into six restarts from zero.
    #
    # NULL means this node run keeps no durable state, which is the default and
    # stays the default. Handing every node a volume nothing writes to is how a
    # deployment accumulates orphans — 3,900 were swept off this one on
    # 2026-08-13.
    checkpoint_volume = Column(String(255), nullable=True)
    # node-version-pinning — the exact build this node actually executed. The
    # digest is stamped at dispatch from the resolved pin (or the current node
    # when unpinned), making a completed run reproducible and auditable:
    # "this NodeRun ran sha256:…", independent of where :latest points now. The
    # digest is also the NodeOutputCache key, so recording it here aligns the
    # execution reference with the cache reference.
    executed_image_digest = Column(String(100), nullable=True)
    node_version_id = Column(ForeignKey("node_versions.id"), nullable=True, index=True)
    # Relationships
    workflow_run = relationship("WorkflowRun", back_populates="node_runs")
    workflow_node = relationship("WorkflowNode")
    node_version = relationship("NodeVersion")


class WorkflowCheckpoint(Base):
    """Gate 3 — how far a node run had got, readable without the host.

    ADVISORY, always. `progress` drives a UI and never a decision: a node that
    reports 0.9 and then fails is still retried, because the only thing that
    proves completion is the task status the executor actually observed. Making
    a self-reported number authoritative would let a node mark itself done.

    `payload` is whatever the node considered worth recording — an epoch, a
    frame index, a file list. The platform does not interpret it; the node
    reads its own back on the next attempt. Storing it here as well as on the
    volume is what lets an operator see where a run got to without shelling
    onto the worker that held it, which on 2026-08-13 was the only way to find
    out why anything had happened.
    """

    __tablename__ = "workflow_checkpoints"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    workflow_run_id = Column(Integer, ForeignKey("workflow_runs.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    node_run_id = Column(Integer, ForeignKey("node_runs.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    # Which attempt reported it. A retry that gets further than its predecessor
    # is the signal Gate 3 exists to produce, and it is unreadable if every
    # checkpoint looks the same.
    attempt = Column(Integer, nullable=False, default=0)
    label = Column(String(255), nullable=True)
    progress = Column(Float, nullable=True)
    payload = Column(JSONB, nullable=True)
    completed_at = Column(DateTime(timezone=True), server_default=func.now(),
                          nullable=False)
    # expr 9.6 (migration 20261012o): soft-delete tombstone stamped by the
    # TTL sweep. NULL = live.
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class ManualInterventionState(Base):
    __tablename__ = "manual_intervention_states"
    id = Column(Integer, primary_key=True)
    node_run_id = Column(ForeignKey("node_runs.id"), unique=True, index=True)
    assigned_to_user_id = Column(ForeignKey("users.id"))
    status = Column(Enum(InterventionStatus), default=InterventionStatus.PENDING_ACTION, index=True)
    instructions = Column(Text)
    completion_notes = Column(Text)
    resume_hook_id = Column(String(255), index=True)
    assigned_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))
    # Relationships
    node_run = relationship("NodeRun")
    assignee = relationship("User")


# =============================================================================
# --- SECTION 7B: WORKFLOW OUTPUT TRACKING & COMPLIANCE
# =============================================================================

class WorkflowNodeOutput(Base):
    """
    Tracks individual outputs from workflow nodes with full provenance.

    Uses content-addressed storage (SHA256 hash) for deduplication.
    Never auto-deleted - retention policy governs hard deletion.

    Lifecycle:
    1. Created when node produces output
    2. is_visible=False when user "deletes" (soft delete)
    3. retention_expires_at determines when hard delete is allowed
    4. legal_hold=True prevents any deletion
    """
    __tablename__ = "workflow_node_outputs"

    id = Column(Integer, primary_key=True)

    # Content reference (pure hash storage)
    content_hash = Column(String(64), nullable=False, index=True)  # SHA256 hash
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)

    # Provenance tracking
    workflow_run_id = Column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    node_run_id = Column(ForeignKey("node_runs.id"), nullable=False, index=True)
    output_index = Column(Integer, default=0, nullable=False)  # For multi-output nodes

    # File metadata
    logical_name = Column(String(512), nullable=True)  # Original filename e.g. 'albedo_output.png'
    file_extension = Column(String(20), nullable=False)  # e.g. 'png', 'exr', 'obj'
    size_bytes = Column(BigInteger, nullable=False)
    mime_type = Column(String(128), nullable=True)

    # Output classification
    is_terminal = Column(Boolean, default=False, nullable=False)  # Final workflow output?
    output_type = Column(String(64), nullable=True)  # 'albedo', 'normal', 'mesh', etc.

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Soft delete / visibility
    is_visible = Column(Boolean, default=True, nullable=False, index=True)
    hidden_at = Column(DateTime(timezone=True), nullable=True)
    hidden_by_user_id = Column(ForeignKey("users.id"), nullable=True)

    # Retention & Compliance
    retention_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)  # When hard delete is allowed
    legal_hold = Column(Boolean, default=False, nullable=False, index=True)
    legal_hold_reason = Column(Text, nullable=True)
    legal_hold_set_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    legal_hold_set_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    tenant = relationship("Tenant")
    workflow_run = relationship("WorkflowRun")
    node_run = relationship("NodeRun")
    hidden_by = relationship("User", foreign_keys=[hidden_by_user_id])
    legal_hold_set_by = relationship("User", foreign_keys=[legal_hold_set_by_user_id])

    # Constraints
    __table_args__ = (
        Index('ix_workflow_node_outputs_content_tenant', 'content_hash', 'tenant_id'),
        Index('ix_workflow_node_outputs_run_node', 'workflow_run_id', 'node_run_id'),
        Index('ix_workflow_node_outputs_retention', 'retention_expires_at', 'legal_hold', 'is_visible'),
    )


class WorkflowOutputAuditLog(Base):
    """
    Append-only audit log for all workflow output operations.

    CRITICAL: This table should NEVER have rows deleted.
    Old records should be archived to cold storage, not deleted.

    Used for:
    - Compliance audits
    - Debugging provenance issues
    - Legal discovery
    """
    __tablename__ = "workflow_output_audit_logs"

    id = Column(Integer, primary_key=True)

    # What was affected
    output_id = Column(Integer, nullable=True, index=True)  # FK to workflow_node_outputs (nullable for deleted outputs)
    content_hash = Column(String(64), nullable=True, index=True)  # Preserved even if output deleted
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)

    # What happened
    action = Column(String(64), nullable=False, index=True)  # 'created', 'hidden', 'legal_hold_set', 'legal_hold_removed', 'hard_deleted', 'archived'

    # Who did it
    performed_by_user_id = Column(ForeignKey("users.id"), nullable=True)  # Null for system actions
    performed_by_system = Column(String(128), nullable=True)  # 'Retention_Cleanup_DAG', 'API', etc.

    # When
    performed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Why / additional context
    reason = Column(Text, nullable=True)
    audit_metadata = Column(JSONB, nullable=True)  # Additional context (old values, request info, etc.)

    # Relationships
    tenant = relationship("Tenant")
    performed_by = relationship("User")

    __table_args__ = (
        Index('ix_audit_log_tenant_time', 'tenant_id', 'performed_at'),
        Index('ix_audit_log_action_time', 'action', 'performed_at'),
    )


class OutputSignature(Base):
    """FX.97 p3a (owner decision Q83=A) — one signed content hash for this
    install: the Ed25519 signature of b"sha256:<content_hash>" made with the
    per-install key held in Vault (services/output_signature.py).

    Key material is NEVER stored here — ``key_id`` is the public handle
    (``mod-out-<hex>``) p3b's verify endpoint resolves. One row per
    (tenant, content_hash, algorithm, key_id): the same bytes re-produced
    under the same install key reuse the row (idempotent), a rotated key
    mints a fresh row, and a foreign signature is never overwritten.

    ISO 27001: no hard delete — a superseded signature is soft-deleted
    (``deleted_at``) and the evidence stays; ``uq_..._live`` uniqueness
    applies only to live rows."""
    __tablename__ = "output_signatures"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)

    content_hash = Column(String(64), nullable=False, index=True)  # SHA256 hex
    algorithm = Column(String(32), nullable=False, index=True)     # 'ed25519'
    key_id = Column(String(128), nullable=False, index=True)       # public handle
    signature_b64 = Column(Text, nullable=False)                   # 64-byte sig, b64
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)

    signed_at = Column(DateTime(timezone=True), nullable=True)

    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    deleted_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        # One live signature per (content_hash, algorithm, key_id) — the
        # tombstone never squats the key, so a rotated install key mints a
        # fresh row and a superseded signature stays as evidence. Same
        # partial-index idiom node_egress_approvals uses.
        Index(
            "uq_output_signatures_live_key",
            "content_hash", "algorithm", "key_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )


# =============================================================================
# --- SECTION 8: MISC & SETTINGS MODELS
# =============================================================================

class Notification(SoftDeleteMixin, Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    recipient_user_id = Column(ForeignKey("users.id"), index=True)
    message = Column(Text)
    is_read = Column(Boolean, default=False, index=True)
    link_to_cog_id = Column(ForeignKey("cogs.id"))
    notification_type = Column(String(50), nullable=True, index=True)  # workflow_complete, workflow_failed, etc.
    link_url = Column(String(500), nullable=True)  # e.g. #logs for navigation
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Soft-hide via Archive action in the bell UI (P8f). Archived
    # rows stay in the DB for audit but are excluded from the
    # default /notifications/list payload.
    is_archived = Column(Boolean, default=False, server_default="false",
                         nullable=False, index=True)
    # GX.S8 — app notification fan-out (routers/notifications.py
    # POST /v1/notifications). severity: info|warning|error|critical.
    # dedupe_key: caller-supplied idempotency key; a second fan-out with
    # the same key for the same tenant reports `deduped: true` instead of
    # creating duplicate rows.
    severity = Column(String(20), nullable=True)
    dedupe_key = Column(String(255), nullable=True, index=True)
    # Relationships
    recipient = relationship("User", back_populates="notifications")


class NotificationPreference(SoftDeleteMixin, Base):
    """notification-preferences (WS4.Q22) — per-user, per-category, per-channel
    delivery preference for notifications.

    ``category`` is the UI-facing grouping (``workflow`` / ``billing`` /
    ``product`` — see ``crud/notification_preferences.py:category_for_type``
    for the mapping from a raw ``Notification.notification_type`` to its
    category), NOT the raw notification_type string. ``channel`` is
    ``in_app`` or ``email``. Absence of a row for a (user, category, channel)
    means "enabled" (the default) — callers must treat a missing row as
    enabled, never as disabled.

    ISO 27001: soft-delete only (SoftDeleteMixin); a preference row is never
    hard-deleted.
    """
    __tablename__ = "notification_preferences"

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    category = Column(String(50), nullable=False)
    channel = Column(String(20), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")

    __table_args__ = (
        Index(
            "uq_notification_pref_user_category_channel",
            "user_id", "category", "channel",
            unique=True,
        ),
    )


class ProjectHandoff(Base):
    """project-agent §6.1/§7 — a durable record of the agent handing work off.

    Two shapes, discriminated by ``kind``:

      - ``question`` (§7) — a parked task's question to the project's user.
        The task could not be finished, so the agent asked and kept working
        on everything else. This row is what lets an answer arriving an hour
        or five weeks later find the parked work again: it carries the
        question, what the agent will do with each possible answer (§7.2),
        the approaches already tried, and the answer once it lands. It is
        raised alongside a ``Notification`` and an email (§7.1).

      - ``wizard_dispatch`` (§6.1) — the node-wizard payload the agent handed
        to a wizard child session. ``project_loop.tick`` builds that payload
        and previously dropped it; recording it here makes the dispatch
        durable and consumable, so the build the payload describes can be
        run and tracked rather than lost the moment the tick returns.

    Tenant-scoped without exception (§8): a handoff belongs to the tenant of
    the project that produced it and is never visible to another tenant.
    """
    __tablename__ = "project_handoffs"

    id = Column(Integer, primary_key=True)
    project_session_id = Column(
        String(36),
        ForeignKey("support_chat_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    task_key = Column(String(200), nullable=True, index=True)
    # question | wizard_dispatch
    kind = Column(String(24), nullable=False, default="question")
    # question:  outstanding | answered | cancelled
    # wizard_dispatch: dispatched | consumed
    status = Column(String(24), nullable=False, default="outstanding",
                    index=True)

    # --- kind="question" -----------------------------------------------
    question = Column(Text, nullable=True)
    # What the agent does with each possible answer (§7.2). A dict so the
    # consequence is structured rather than buried in prose.
    consequences = Column(JSONB, nullable=True)
    # The approaches already tried, kept next to the question — an answer
    # three weeks later is only useful beside the record of what it answers.
    tried = Column(JSONB, nullable=True)
    answer = Column(Text, nullable=True)
    notification_id = Column(ForeignKey("notifications.id", ondelete="SET NULL"),
                             nullable=True)

    # --- kind="wizard_dispatch" ----------------------------------------
    # The node_wizard_payload handed to the wizard (§6.1). Expected input,
    # the process, expected output — never the project's transcript.
    payload = Column(JSONB, nullable=True)
    child_session_id = Column(
        String(36),
        ForeignKey("support_chat_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    rung = Column(String(24), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    answered_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_project_handoff_outstanding",
              "project_session_id", "status"),
    )


# =============================================================================
# --- conversational-project-builder §1.1–1.3 — man-in-the-middle collab loop
#
# Three project-scoped tables that layer the conversational builder on top of
# the existing project-agent (a SupportChatSession with kind="project"). They
# extend that project — they do NOT introduce a parallel Project concept — the
# same way ProjectHandoff above already hangs off support_chat_sessions.id.
#
# All three are tenant-scoped without exception (project-agent §8) and, per
# ISO 27001, APPEND-ONLY / SOFT-DELETE ONLY: rows are never physically
# removed. UPDATE is permitted (status transitions, intent override, setting
# deleted_at); a BEFORE DELETE (row) + BEFORE TRUNCATE (statement) trigger
# installed by the migration raises on any hard delete — the same enforcement
# wizard_runs uses. Removal is always a soft-delete via ``deleted_at``.
# =============================================================================

class ProjectConversationTurn(Base):
    """conversational-project-builder §1.1 — one turn in a project's
    align-before-build conversation.

    The conversational surface is where a project starts: the user and the
    system align on WHAT to build before any checklist is generated or any
    build work dispatched. Every turn — the user's asks, the system's
    clarifying questions and hand-offs — is persisted here on the project so
    the whole collaboration lives inside MOD and remains retrievable for the
    life of the project (§ "Conversation is persisted with the project").

    Distinct from ``support_chat_messages``: those are the raw multi-turn
    support/wizard transcript; this is the project-builder collaboration
    record, which additionally carries the classified ``intent`` that routes
    the project (approve | infra-broken | rework | change-scope) and is
    append-only / soft-delete only.
    """
    __tablename__ = "project_conversation_turns"

    # BigInteger on Postgres, Integer on SQLite — same rationale as
    # SupportChatMessage: a project may accumulate many turns.
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    project_session_id = Column(
        String(36),
        ForeignKey("support_chat_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    # Who produced the turn — a user id, "system", or "agent". Kept beside
    # ``role`` so the display author survives even when role is coarse.
    author = Column(String(64), nullable=False)
    # user | system | agent
    role = Column(String(16), nullable=False)
    body = Column(Text, nullable=False)
    # The classified intent for a user feedback turn:
    # approve | infra-broken | rework | change-scope. NULL for system turns
    # and for user turns not (yet) classified. Ambiguity is NEVER stored as
    # ``approve`` (a wrong approve is the costly failure mode).
    intent = Column(String(24), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    # Soft-delete tombstone (ISO 27001: no hard delete of project history).
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_project_turn_session_created",
              "project_session_id", "created_at"),
    )


class ProjectChecklistTask(Base):
    """conversational-project-builder §1.2 — one task on a project's shared,
    opsx-style checklist.

    Agreement in the conversation produces an ordered list of discrete,
    testable tasks that both the user and the system see and tick. Each task
    maps to a buildable checkpoint and carries a status reflecting its
    checkpoint lifecycle. The list is edited IN PLACE as the project
    progresses: a ``rework`` reopens a task (``reworking``), a
    ``change-scope`` adds / re-orders / soft-deletes tasks. Removals are
    soft-delete only; checklist history is never hard-deleted (ISO 27001).
    """
    __tablename__ = "project_checklist_tasks"

    id = Column(Integer, primary_key=True)
    project_session_id = Column(
        String(36),
        ForeignKey("support_chat_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    # Position in the shared list. Ordering is explicit (not by id) so
    # change-scope can re-order without rewriting rows.
    order_index = Column(Integer, nullable=False, default=0)
    # A short, stable handle for the task (e.g. "login-page"), so a
    # checkpoint / handoff can reference the task independently of its id.
    task_key = Column(String(200), nullable=True, index=True)
    title = Column(Text, nullable=False)
    # Optional longer body / acceptance detail for the task.
    body = Column(Text, nullable=True)
    # todo | building | awaiting-test | accepted | reworking
    status = Column(String(24), nullable=False, default="todo", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    # UPDATE is permitted (this is a status-transitioning row); the
    # append-only trigger forbids only physical DELETE/TRUNCATE.
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)
    # Soft-delete tombstone (change-scope drops, ISO 27001).
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_project_checklist_session_order",
              "project_session_id", "order_index"),
    )


class ProjectCheckpoint(Base):
    """conversational-project-builder §1.3 — the structured hand-off that
    links a checklist task to the concrete thing the user is asked to test.

    A checkpoint is the gate: after the system builds a task's meaningful
    unit it STOPS and hands it off with a concrete "test this: <link> —
    check <thing>". The hand-off payload is structured on purpose — a
    hand-off with no openable artifact is not allowed — so it always carries
    an ``artifact_link`` and a ``what_to_check``, plus a reference to the
    produced node / stack / workflow / page in ``artifact_ref``.

    One live checkpoint per checklist task at a time; a ``rework`` produces a
    new hand-off for the same task rather than mutating history. Soft-delete
    only (ISO 27001).
    """
    __tablename__ = "project_checkpoints"

    id = Column(Integer, primary_key=True)
    checklist_task_id = Column(
        Integer,
        ForeignKey("project_checklist_tasks.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Denormalised onto the checkpoint so a project's hand-offs can be listed
    # and tenant-scoped without joining through the checklist.
    project_session_id = Column(
        String(36),
        ForeignKey("support_chat_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    # The produced artifact reference: the node / stack / workflow / page /
    # run this checkpoint built. {"type": "node"|"stack"|..., "id": ...}.
    artifact_ref = Column(JSONB, nullable=True)
    # The openable "test this" link. Required in practice — a hand-off with
    # nothing to open is rejected by the (later) gate logic; kept nullable at
    # the column level only so an in-progress row can be assembled.
    artifact_link = Column(Text, nullable=True)
    # What specifically to verify (e.g. "check the login button submits").
    what_to_check = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)
    # Soft-delete tombstone (ISO 27001).
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_project_checkpoint_task",
              "checklist_task_id", "created_at"),
    )


class Comment(Base):
    __tablename__ = "comments"
    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id"))
    tenant_id = Column(ForeignKey("tenants.id"), index=True)
    target_id = Column(Integer, index=True)
    target_type = Column(String(50), index=True)
    parent_comment_id = Column(ForeignKey("comments.id"))
    content = Column(Text)
    mentions = Column(JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    # Relationships
    author = relationship("User")
    replies = relationship("Comment", cascade="all, delete-orphan")


class TenantSetting(Base):
    __tablename__ = "tenant_settings"
    tenant_id = Column(ForeignKey("tenants.id"), primary_key=True)
    key = Column(String(100), primary_key=True)
    value = Column(JSONB)
    # Relationships
    tenant = relationship("Tenant", back_populates="settings")


class UserSetting(Base):
    __tablename__ = "user_settings"
    user_id = Column(ForeignKey("users.id"), primary_key=True)
    key = Column(String(100), primary_key=True)
    value = Column(JSONB)
    # Relationships
    user = relationship("User", back_populates="settings")


# =============================================================================
# --- SECTION 9: BILLING, AUDITING & LOGGING MODELS
# =============================================================================

# OWNER-DECISIONS 10-14 10.4 §2 (rec B): overage rate floor, ~$0.025/credit.
# Both overage-rate columns below store whole cents, and there is no
# integer value between 2 and 3, so the floor is enforced at 3 (= $0.03).
OVERAGE_RATE_FLOOR_CENTS = 3


class Plan(SoftDeleteMixin, Base):
    __tablename__ = "plans"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True)
    stripe_price_id = Column(String(255), unique=True, nullable=True)  # Monthly billing — nullable for free tier and custom plans
    # launch-pricing-lockdown §4 — annual billing variant. 17% discount
    # encoded as 10× monthly price (10 months for the price of 12).
    # Nullable on tiers without an annual offering (Free, Enterprise).
    stripe_price_id_annual = Column(String(255), unique=True, nullable=True)

    # Seat limits
    max_seats = Column(BigInteger, nullable=False, default=0)

    # Storage limits (in GB)
    max_storage_gb = Column(BigInteger, nullable=False, default=0)

    # Credit limits
    credits_per_month = Column(BigInteger, nullable=False, default=0)

    # Process limits
    max_concurrent_processes = Column(BigInteger, nullable=False, default=1)

    # CPU hours quota (#89) — max container CPU-hours per billing month. 0 = unlimited.
    max_cpu_hours_monthly = Column(BigInteger, nullable=False, default=0, server_default='0')

    # Pricing
    price_in_cents = Column(Integer)
    overage_credit_price_cents = Column(Integer, nullable=True)  # Per-credit overage rate (null = no overage allowed)

    # Egress / bandwidth (add-egress-billing). included_egress_gb = monthly
    # bytes-OUT allowance; egress beyond it bills as overage at
    # egress_overage_price_cents per GB (null = no egress overage allowed → the
    # metering job can warn/cap instead of charging). Metered from MinIO's
    # per-bucket traffic_sent_bytes (the tenant-{id}-files bucket).
    included_egress_gb = Column(BigInteger, nullable=False, default=0, server_default='0')
    egress_overage_price_cents = Column(Integer, nullable=True)  # per-GB egress overage rate

    # DERP relay / bandwidth (tailnet-plan Group 8). included_relay_gb = monthly
    # MOD-relayed-bytes allowance; relay beyond it bills as overage at
    # relay_overage_price_cents per GB (null = no relay overage → the metering job
    # can warn/cap instead of charging). Metered from a MOD-operated DERP's
    # per-account byte counters; direct P2P and customer-DERP traffic never accrue.
    included_relay_gb = Column(BigInteger, nullable=False, default=0, server_default='0')
    relay_overage_price_cents = Column(Integer, nullable=True)  # per-GB relay overage rate
    # Whether this plan is entitled to use MOD's DERP relay at all (tailnet-plan
    # task 7.9). The account-service verify callback gates relay on this + standing
    # + overage cap; pricing above meters the usage. Default false = not entitled.
    relay_entitled = Column(Boolean, nullable=False, default=False, server_default='false')

    # Per-plan default cap (cents) on accrued overage per billing period — a
    # runaway/surprise-bill backstop. Resolution order (utils/overage_cap):
    # tenant.monthly_overage_cap_cents → this → billing_settings.default →
    # NULL (uncapped). NULL here means "fall through to the global default";
    # unlimited tiers (Enterprise/Platform) and Free stay uncapped.
    overage_cap_cents = Column(Integer, nullable=True)

    # Feature flags
    allow_custom_registry = Column(Boolean, default=False, server_default='f')
    is_free_tier = Column(Boolean, default=False, server_default='f')
    is_custom_plan = Column(Boolean, default=False, server_default='f')  # Custom plan for specific tenant

    # Custom plan metadata
    custom_for_tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)  # If custom, which tenant?
    custom_description = Column(Text, nullable=True)  # Notes about custom deal

    # Rate limiting (0 = unlimited)
    api_rate_limit_per_minute = Column(Integer, default=200, server_default='200', nullable=False)
    # LLM CALL volume, distinct from the HTTP request rate above. One wizard
    # request fans out into many LLM calls (several critic passes), so bounding
    # requests does not bound LLM volume. 0 = unlimited, matching the
    # convention of api_rate_limit_per_minute so both knobs read the same way.
    llm_calls_per_hour = Column(Integer, default=0, server_default='0', nullable=False)
    # NULL = inherit the platform default. Concurrency protects the shared
    # model server from a burst regardless of what a customer bought, so unlike
    # volume it is not primarily a plan concept — but a plan may raise it.
    llm_concurrent_max = Column(Integer, nullable=True)

    # Pro feature gates
    allow_custom_nodes = Column(Boolean, default=False, server_default='f', nullable=False)
    show_timing = Column(Boolean, default=False, server_default='f', nullable=False)

    # Identity & Access feature gates (SSO + SCIM). Columns are added by the
    # model-less migrations 20260605_add_sso_enabled_to_plans / _scim_*; mapped
    # here so the ORM (create/PATCH) can read+write them, not just raw SQL.
    sso_enabled = Column(Boolean, default=False, server_default='f', nullable=False)
    scim_enabled = Column(Boolean, default=False, server_default='f', nullable=False)

    # clean-v2-api Wave 10 (tier-enforcement gap #1) — optional commercial-tier
    # bundle: one of the tier names utils.entitlements defines. When set,
    # `entitlements_from_plan`
    # grants that tier's WHOLE Cap.* vocabulary (mesh federation, burst,
    # predictive, GPU-pool mgmt, multi-tenant, SCIM, ...) in addition to the
    # boolean columns above, so a SaaS plan expresses the same
    # capabilities as an on-prem license at that tier. NULL (every pre-existing
    # plan) means "no bundle" — behavior is unchanged unless an operator opts
    # a plan in explicitly.
    tier = Column(String(20), nullable=True)

    # Trial
    free_trial_days = Column(Integer, default=0, server_default='0', nullable=False)  # Days of free trial (0 = no trial)

    # Display/Marketing
    display_order = Column(Integer, default=0)  # For ordering on pricing page
    is_popular = Column(Boolean, default=False, server_default='f')  # "Most Popular" badge
    is_visible = Column(Boolean, default=True, server_default='t')  # Hide custom plans from public pricing page

    # Phase 11 — per-plan perf-gate acceptance threshold. Ratio of
    # CPU reference latency to variant latency required for the variant
    # to be flipped to `verified`. Null → use the module default
    # (0.25 = "variant must be at least 25% as fast as CPU"). Plans
    # for GPU-heavy tiers may want to tighten this to 1.0 or higher.
    acceptance_perf_ratio = Column(Float, nullable=True)

    # sz-payg.2 (payg-billing-hardening §1.2) — per-plan overage rates for the
    # new seat/storage/concurrent PAYG meters. SHIPS SHADOW: these only expose
    # rates; nothing reads or charges against them yet (see
    # migrations/20261009h_payg_plan_overage_columns.py).
    overage_seat_price_cents = Column(
        Integer, nullable=False, default=1500, server_default=text("1500")
    )
    overage_storage_price_cents_per_gb_month = Column(
        Integer, nullable=False, default=75, server_default=text("75")
    )
    overage_concurrent_price_cents = Column(
        Integer, nullable=False, default=2500, server_default=text("2500")
    )

    @validates("overage_credit_price_cents")
    def _validate_overage_credit_price_cents(self, key, value):
        if value is not None and value < OVERAGE_RATE_FLOOR_CENTS:
            raise ValueError(
                f"overage_credit_price_cents must be >= {OVERAGE_RATE_FLOOR_CENTS}"
                f" (~$0.025/credit floor); got {value}"
            )
        return value

    # Relationships
    subscriptions = relationship("Subscription", back_populates="plan", foreign_keys="[Subscription.plan_id]")
    custom_tenant = relationship("Tenant", foreign_keys=[custom_for_tenant_id], backref="custom_plan")


class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), unique=True)
    plan_id = Column(ForeignKey("plans.id"))

    # Stripe integration
    stripe_subscription_id = Column(String(255), unique=True, index=True, nullable=True)  # Nullable for free tier
    stripe_customer_id = Column(String(255), index=True, nullable=True)  # Nullable for free tier

    # Subscription status (active, past_due, canceled, trialing, etc.)
    status = Column(String(50), index=True)

    # Trial tracking
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    is_trial = Column(Boolean, default=False, server_default='f')

    # Billing periods
    current_period_start = Column(DateTime(timezone=True), nullable=True)
    current_period_ends_at = Column(DateTime(timezone=True), nullable=True)

    # Idempotency and payment tracking
    last_invoice_id = Column(String(255), nullable=True)  # Track last processed invoice for idempotency

    # Failed payment handling
    payment_failed_at = Column(DateTime(timezone=True), nullable=True)
    grace_period_ends = Column(DateTime(timezone=True), nullable=True)
    payment_failure_count = Column(Integer, default=0, server_default='0')

    # #103: Credit overage opt-in
    allow_overage = Column(Boolean, default=False, server_default='f', nullable=False)

    # #101: Scheduled plan change (downgrade deferred to end of billing period)
    pending_plan_id = Column(Integer, ForeignKey("plans.id"), nullable=True)
    pending_plan_change_at = Column(DateTime(timezone=True), nullable=True)

    # GSEC FX.92 (Q78=A) — hosted cancellation export-window state
    # (services/cancellation_lifecycle.py): {"status": "window"|"reactivated"
    # |"deleted", "started_at", "window_days", "ends_at",
    # "reminders_sent": [day labels]}. NULL = never canceled. Read/written
    # as a dict only — NEVER via .astext.
    cancellation_window = Column(JSONB, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    tenant = relationship("Tenant", back_populates="subscription")
    plan = relationship("Plan", back_populates="subscriptions", foreign_keys=[plan_id])
    pending_plan = relationship("Plan", foreign_keys=[pending_plan_id])


class AuditEvent(Base):
    __tablename__ = "audit_events"
    # BigInteger().with_variant(Integer, "sqlite") so the test suite's
    # in-memory engine can autoincrement the primary key via ROWID.
    # Postgres still gets BIGINT + the existing nextval('audit_events_id_seq')
    # default; column rendering switch only, no data-shape change.
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), index=True)
    user_id = Column(ForeignKey("users.id"), index=True)
    event_name = Column(String(100), index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    details = Column(JSONB)


class SecurityAlert(Base):
    """GSEC FX.156 part 1 (owner decision Q143=B) — platform security
    alert-rules engine (services/security_alert_engine.py).

    One row per raised alert, deduped by (rule_key, tenant_id, subject):
    repeated findings for the same subject INCREMENT count + refresh
    last_seen_at instead of creating new rows. ``tenant_id`` NULL =
    platform-level alert (the on-prem customer-security-contact path is
    identical — same engine, same table).

    ISO 27001: NO hard delete — removal is a STATUS change that stamps
    ``deleted_at``/``deleted_by``; the row (and its evidence) always
    stays. Acknowledgement (part 2 endpoint) stamps
    ``acked_at``/``acked_by`` and records the acknowledgement time.
    """
    __tablename__ = "security_alerts"
    # Same BigInteger/Integer variant trick as AuditEvent so the test
    # suite's in-memory-style engine can autoincrement via ROWID while
    # Postgres keeps BIGINT.
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # Stable rule key (models/services.security_alert_engine.RULES) —
    # "signin_failures", "admin_role_change", ... (more rules in parts 3+).
    rule_key = Column(String(64), nullable=False, index=True)
    # low | medium | high | critical.
    severity = Column(String(16), nullable=False,
                      default="high", server_default="high")
    # NULL = platform-level alert (audit row carried no tenant).
    tenant_id = Column(ForeignKey("tenants.id"), nullable=True, index=True)
    # Human-readable subject: "user:<id>" / "ip:<addr>" /
    # "identity:<prefix>" for sign-in failures; "<action>:user:<id>"
    # for admin/role-grant actions.
    subject = Column(String(255), nullable=False)
    # Evidence payload (window counts, first/last timestamps, actor ids,
    # source IP) — NEVER credentials, tokens or secrets.
    details = Column(JSONB)
    first_seen_at = Column(DateTime(timezone=True), nullable=False,
                           server_default=func.now(), index=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=False,
                          server_default=func.now(), index=True)
    count = Column(Integer, nullable=False, default=1, server_default="1")
    # Part 2: acknowledgement (records acknowledgement time + who).
    acked_at = Column(DateTime(timezone=True), nullable=True)
    acked_by = Column(ForeignKey("users.id"), nullable=True)
    # Soft delete ONLY (ISO 27001 — no hard deletes).
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        Index("ix_security_alerts_dedupe", "rule_key", "tenant_id",
              "subject"),
        CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="ck_security_alerts_severity"),
    )


class SecurityAlertRuleState(Base):
    """GSEC FX.156 part 1 — per-rule engine cursor.

    ``last_run_at`` = the audit-timestamp boundary of the last
    successful evaluation: rules consume audit rows strictly after it,
    so a restart never re-scans (and re-counts) already-consumed events.
    First run for a rule looks back 1 hour (services/
    security_alert_engine.FIRST_RUN_LOOKBACK). Rows are never deleted.
    """
    __tablename__ = "security_alert_rule_state"
    rule_key = Column(String(64), primary_key=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class TenantSecurityAlertRuleConfig(Base):
    """GSEC FX.112 p3a — per-tenant override of an FX.156 built-in
    security alert rule.

    A tenant admin lists the built-in rules that apply to their tenant
    (routers/tenant/security.py GET /security/alert-rules) and, per
    rule, flips ``enabled`` and/or sets ``threshold_override`` within
    the platform-set min/max bounds (``security_alerts.tenant_*``
    platform settings, services/tenant_alert_rules.py). The FX.156
    engine (services/security_alert_engine.run_rules) consults these
    rows on every sweep: a disabled tenant gets no alerts from that
    rule; a threshold override replaces the platform default for
    threshold-tunable rules (signin_failures / unusual_downloads /
    export_anomaly) only. ``threshold_override`` NULL = "use the
    platform default" (the rule is still enabled).

    Rows are UPDATED in place (one live row per (tenant_id, rule_key),
    unique) — never deleted (ISO 27001); every change is audited
    (AuditAction.TENANT_SECURITY_ALERT_RULE_UPDATED).
    """
    __tablename__ = "tenant_security_alert_rule_configs"
    # Same BigInteger/Integer variant trick as SecurityAlert so the test
    # suite's in-memory-style engine can autoincrement via ROWID while
    # Postgres keeps BIGINT.
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    # Stable FX.156 rule key (services/security_alert_engine.RULES).
    rule_key = Column(String(64), nullable=False, index=True)
    # Rule on/off for THIS tenant (no row = platform default, enabled).
    enabled = Column(Boolean, nullable=False,
                     default=True, server_default="true")
    # NULL = use the platform default threshold; otherwise the tenant's
    # per-rule threshold within the platform min/max bounds.
    threshold_override = Column(Integer, nullable=True)
    updated_by = Column(ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "rule_key",
                         name="uq_tenant_security_alert_rule_configs"),
    )


class WizardEventLog(Base):
    __tablename__ = "wizard_event_log"
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    event_type = Column(String(60), nullable=False, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=True, index=True)
    build_spec_id = Column(ForeignKey("build_specs.id"), nullable=True)
    workflow_run_id = Column(ForeignKey("workflow_runs.id"), nullable=True)
    metadata_ = Column("metadata", JSONB, default={})
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class PlatformPaymentSettings(Base):
    """
    Platform-level payment provider credentials.

    CMMC/ISO 27001 Compliance:
    - SC-13: Credentials encrypted at rest using Fernet (AES-128-CBC)
    - AU-2: All changes are audit logged via updated_by_user_id
    - AC-6: Only platform admins can modify (enforced at API layer)

    Single row table - stores encrypted Stripe API credentials.
    These are NOT stored in .env to prevent accidental version control exposure.

    Note: Stripe Price IDs are stored in the Plan table, not here.
    """
    __tablename__ = "platform_payment_settings"

    id = Column(Integer, primary_key=True, index=True)

    # Payment provider (currently only Stripe supported)
    provider = Column(String(50), default="stripe", nullable=False)

    # Encrypted Stripe credentials
    # SECURITY: Encrypted using Fernet (same key as tenant secrets)
    encrypted_secret_key = Column(Text, nullable=True)  # sk_live_... or sk_test_...
    encrypted_publishable_key = Column(Text, nullable=True)  # pk_live_... or pk_test_...
    encrypted_webhook_secret = Column(Text, nullable=True)  # whsec_...

    # Configuration flags
    is_test_mode = Column(Boolean, default=True, nullable=False)  # True = test keys, False = live keys
    is_configured = Column(Boolean, default=False, nullable=False)  # True once all keys are set

    # Audit trail
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


class CaptureThresholdSet(Base):
    """Rule bands for one (detector, working_resolution_px) pair.

    PARAMETERISED ON PURPOSE. The field client runs ORB on a ~1024px proxy;
    `capture-preflight` runs SIFT on the original. The same number meets
    feature populations an order of magnitude apart, so a single global
    threshold cannot serve both — serving the server's raw values to an ORB
    client is the obvious implementation and produces a confidently wrong
    answer in the field.

    So the server's own values are simply another row here, and there is no
    "derivation" code path that could silently fall back: a client asking for a
    pair nobody has derived gets a 409, not an approximation.

    Bands, not numbers. Each rule carries `clearly_thin_below` and
    `looks_covered_at_or_above`; between them the device says UNCLEAR and
    defers. Widening that gap is the safety dial.
    """
    __tablename__ = "capture_threshold_sets"

    id = Column(Integer, primary_key=True)
    # Opaque to the client, used as its ETag. Changes whenever the bands do,
    # which is how a device knows its cache is stale without re-downloading.
    config_id = Column(String(64), nullable=False, unique=True, index=True)
    detector = Column(String(32), nullable=False)
    working_resolution_px = Column(Integer, nullable=False)
    # {"keypoint_floor": {...}, "keypoint_density": {...}, "track_length": {...}}
    rules = Column(JSONB, nullable=False)
    # Which set these were derived FROM, for auditability. NULL on the
    # authoritative server row, which is derived from nothing.
    derived_from = Column(String(64), nullable=True)
    issued_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        # One ACTIVE set per pair, ENFORCED. This was documented as an
        # invariant and left as a plain index, so two active ORB@1024 rows
        # would have been accepted and ORDER BY id DESC would quietly pick
        # one — deterministic, and still a convention rather than a rule.
        # Partial unique index so superseded (is_active=false) rows can stay
        # for audit.
        Index('ix_capture_thresholds_active_pair', 'detector',
              'working_resolution_px', unique=True,
              postgresql_where=text('is_active')),
    )


class CaptureDevice(Base):
    """A field device. DELIBERATELY NOT A WorkerLicense.

    A worker claims from ApiTaskQueue, heartbeats every 15s and is FENCED after
    90s of silence. A device whose entire purpose is operating where there is
    no signal would be permanently fenced and permanently stale — see
    `capture-field-client` design.md Decision 2.

    The token must survive weeks between syncs, because that is a normal field
    pattern, so it carries no short expiry an operator could discover on site.
    It is scoped to one tenant and to the capture endpoints only.
    """
    __tablename__ = "capture_devices"

    id = Column(Integer, primary_key=True)
    device_id = Column(String(64), nullable=False, unique=True, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    enrolled_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    device_name = Column(String(255), nullable=True)
    token_hash = Column(String(128), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class MobileDevice(Base):
    """A MOBILE DEVICE enrolled against a tenant (CUIP.3, FEAT.CUI-PHONES).

    The phone half of CUI on phones (DECIDED B shelf + DECIDED C severity tiers,
    cui-phones.md): a capture FIELD CLIENT is signal-independent and long-lived
    (see CaptureDevice above), a phone is neither — it has signal, it carries a
    build, and its owner can be revoked. So this table records the enrollment
    itself: which device, which platform, which app build, the HASH of its
    device token (never the token), and — when the phone reported one — the
    attestation/integrity tier it currently holds (NULL = none yet).

    `device_id` is CLIENT-generated and UNIQUE: the app picks it once at
    enrollment and re-presents it on every sync, so a re-enrollment of the same
    phone must land on this row rather than mint a second one.

    NEW TABLE ONLY (models.py is bind-mounted live and shared by every lane: no
    column was added to any existing table). SOFT REVOKE ONLY (ISO 27001 A.18.1.3,
    protection of records): `revoked_at` is a marker, never a removal, and this
    table has no DELETE path at all. `tenant_id` is NOT NULL with NO ON DELETE —
    tenants are never hard-deleted (ISO soft delete), so the owner link must
    never be nulled; `enrolled_by_user_id` DOES carry ON DELETE SET NULL, because
    losing the enrolling user must never erase the device record.

    `id` is BIGSERIAL in the live DB (migration
    20261009a_cui_phones_mobile_devices.py); the sqlite variant keeps the test
    suite's engine autoincrementing the key (same trick as AuditEvent).
    """
    __tablename__ = "mobile_devices"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    device_id = Column(String(64), nullable=False, unique=True)
    platform = Column(String(16), nullable=False)
    app_build = Column(String(64), nullable=False)
    token_hash = Column(String(128), nullable=False)
    integrity_tier = Column(String(16), nullable=True)
    enrolled_by_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"),
                                 nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)


class MobileCuiShelfItem(Base):
    """A CUI PHONE SHelf item — staged asset awaiting transfer to an enrolled phone.

    CUIP.20 (FEAT.CUI-PHONES): a "shelf" for assets a user has marked as staged
    for transfer to one enrolled phone.  This is a secure staging area — soft
    revoke only (ISO 27001).

    `tenant_id` is NOT NULL with NO ON DELETE: tenants are never hard-deleted
    (ISO soft delete), so the owner link must never be nulled — a tenant delete
    trying to erase it would fail the NOT NULL, which is the point.
    `device_id` carries ON DELETE SET NULL (two words): losing the phone must
    never erase the shelf record.  Same for `staged_by_user_id`.
    Revoke is a soft marker (`revoked_at`), never a real removal: this table
    has no DELETE path at all; downgrade is a NO-OP.

    `id` is BIGSERIAL in the live DB (migration
    20261008b_cui_phones_mobile_cui_shelf.py); the sqlite variant keeps the test
    suite's engine autoincrementing the key (same trick as AuditEvent).
    """
    __tablename__ = "mobile_cui_shelf"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    device_id = Column(BigInteger().with_variant(Integer, "sqlite"),
                       ForeignKey("mobile_devices.id", ondelete="SET NULL"),
                       nullable=True, index=True)
    resource_type = Column(String(64), nullable=False)
    resource_id = Column(String(128), nullable=False)
    staged_by_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"),
                               nullable=True, index=True)
    staged_at = Column(DateTime(timezone=True), server_default=func.now(),
                       nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)
    size_bytes = Column(BigInteger().with_variant(Integer, "sqlite"),
                        nullable=True)


class CaptureVerdict(Base):
    """One on-site coverage verdict, as reported by a device.

    ADVISORY. `capture-preflight` produces the verdict recorded against a run;
    this is what the operator was told in the field, kept so the two can be
    compared afterwards — which is also the only way the derived bands ever get
    calibrated.

    `verdict_id` is CLIENT-generated and unique, because the outbox is
    at-least-once by design: it retries until it gets a 2xx, and driving out of
    signal mid-sync is the normal field case rather than an edge one. Ingest
    upserts on it.

    Stands alone rather than hanging off a WorkflowRun: on site there is no run
    yet, the images are still on the card. `capture_session_id` is what a later
    upload can be reconciled against.
    """
    __tablename__ = "capture_verdicts"

    id = Column(Integer, primary_key=True)
    # UNIQUE PER TENANT, NOT GLOBALLY. verdict_id is chosen by the device, so a
    # global constraint lets one tenant's id collide with another's — and the
    # client, behaving exactly as specified, reads the resulting 200/duplicate
    # as success and DROPS the outbox entry. That tenant's verdict is then
    # silently gone, and nobody can reproduce it because the images are on a
    # card in someone's bag. UUIDv4 collision is negligible; a client-chosen
    # key under a global constraint is the actual problem.
    verdict_id = Column(String(64), nullable=False, index=True)
    device_id = Column(String(64), nullable=False, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    capture_session_id = Column(String(64), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    produced_at = Column(DateTime(timezone=True), nullable=False)
    thresholds_config_id = Column(String(64), nullable=False)
    thresholds_stale = Column(Boolean, nullable=False, default=False)
    overall = Column(String(20), nullable=False)
    summary = Column(Text, nullable=True)
    image_count = Column(Integer, nullable=False)
    rules = Column(JSONB, nullable=False)
    untexturable_images = Column(JSONB, nullable=True)
    unmatched_images = Column(JSONB, nullable=True)
    weak_regions = Column(JSONB, nullable=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('tenant_id', 'verdict_id', name='uq_capture_verdicts_tenant_verdict'),
    )


class CapturePreflightReport(Base):
    """Persisted SERVER-side capture-preflight report, keyed on the workflow run.

    Distinct from CaptureVerdict (which is the FIELD CLIENT's on-device
    approximation, keyed on a device-chosen verdict_id). This row is the
    authoritative COLMAP-node verdict for one preflight WorkflowRun: the node
    (CORE/DockerContainers/capture-preflight) emits it as sentinel-fenced JSON on
    stdout AND as the `preflight_report.json` output; the API persists it here on
    node completion so it is queryable without re-parsing the fenced log tail.

    `database_object_key` / `sparse_ply_object_key` reference the node's
    `database.db` and `sparse.ply` outputs (in the run's output storage). The
    database key is the REUSE handle: a capture that proceeds to full
    reconstruction feeds this COLMAP database forward instead of recomputing
    features + matches.
    """
    __tablename__ = "capture_preflight_reports"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    workflow_run_id = Column(ForeignKey("workflow_runs.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    node_run_id = Column(Integer, nullable=True, index=True)
    # "pass" | "warn" | "error" (advisory — a capture is never hard-blocked).
    verdict = Column(String(20), nullable=False)
    advisory = Column(Boolean, nullable=False, default=True)
    summary = Column(JSONB, nullable=True)        # images_submitted / _registered / points_reconstructed
    findings = Column(JSONB, nullable=True)       # each: {kind, detail, remedy, images/regions}
    weak_regions = Column(JSONB, nullable=True)   # under-observed 3D points for the highlighted render
    thresholds = Column(JSONB, nullable=True)     # the (provisional) config the verdict used
    # Reuse handles — the node's preserved outputs.
    database_object_key = Column(String(512), nullable=True)
    sparse_ply_object_key = Column(String(512), nullable=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('workflow_run_id', name='uq_capture_preflight_run'),
    )


class InfrastructureCost(SoftDeleteMixin, Base):
    """Fixed platform infrastructure that no single worker accounts for.

    Per-worker cost prices the machines that RUN tasks. The manager gets its
    own line. Everything else that has to exist for either to work — the
    registry host, switches, the UPS, rack space, the internet line — was
    simply absent from the model, so its cost was silently absorbed.

    TWO SHAPES, because infrastructure comes in two shapes. A switch is
    CAPITAL: a price, amortised, plus what it draws. A transit line or colo
    contract is RECURRING: a monthly bill with no capital and no wattage of
    its own. Forcing the second into the first is how you end up amortising a
    bandwidth invoice over sixty months.

    Not to be confused with EGRESS, which is usage-based and already metered
    per-tenant from MinIO (`tenant_egress_usage_daily`). This is the fixed
    floor you pay whether anyone runs anything or not.
    """
    __tablename__ = "infrastructure_costs"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    # "capital" (amortised hardware) or "recurring" (a monthly bill).
    kind = Column(String(20), nullable=False, default="capital")

    # capital
    hardware_cost_cents = Column(BigInteger, nullable=True)
    amortisation_months = Column(Integer, nullable=True)
    power_watts = Column(Integer, nullable=True)
    electricity_cents_per_kwh = Column(Integer, nullable=True)

    # recurring
    monthly_cents = Column(BigInteger, nullable=True)

    # Derived, stored so the figure is auditable rather than recomputed
    # differently by whoever reads it next.
    cost_per_hour_cents = Column(Integer, nullable=True)

    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TenantPricingOverride(SoftDeleteMixin, Base):
    """Per-tenant override of the global credit prices (postpaid pay-as-you-go
    and subscription overage). Set by a platform admin from Pricing Config →
    Tenant Pricing. One row per tenant; NULL on a price field = use the global
    BillingSettings value. `valid_until` NULL = no expiry; a past value means
    the override is ignored and the global price applies again."""
    __tablename__ = "tenant_pricing_overrides"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, unique=True, index=True)
    override_postpaid_price_cents = Column(Integer, nullable=True)
    override_overage_price_cents = Column(Integer, nullable=True)
    reason = Column(Text, nullable=True)
    valid_until = Column(DateTime(timezone=True), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class EnterpriseLead(Base):
    """A sales lead captured from the pricing page's Enterprise 'Contact Sales'
    form. Stored so leads aren't lost if the notification email fails."""
    __tablename__ = "enterprise_leads"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(255), nullable=True)
    email = Column(String(255), nullable=False)
    company = Column(String(255), nullable=True)
    message = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, server_default="new")  # new / contacted / closed
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Q17 (owner decision, 2026-10-03) — sales-lead retention: stamped by the
    # daily leads_retention_tick when the lead's personal fields (name, email,
    # message) are erased. NULL = PII still present. The row itself always
    # stays (ISO 27001); id/dates/status/tenant/company remain for sales stats.
    pii_scrubbed_at = Column(DateTime(timezone=True), nullable=True)


class OverageCapRequest(Base):
    """A tenant's request to raise their monthly overage spend cap. Created from
    the tenant billing UI ("Request a higher limit") and resolved by a platform
    admin (approving bumps Tenant.monthly_overage_cap_cents)."""
    __tablename__ = "overage_cap_requests"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    current_cap_cents = Column(Integer, nullable=True)       # effective cap snapshot at request time
    requested_cap_cents = Column(Integer, nullable=False)    # what the tenant is asking for
    reason = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="pending", server_default="pending")  # pending|approved|denied
    admin_note = Column(Text, nullable=True)
    resolved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class LifecycleEmailLog(Base):
    """activation-lifecycle-emails: one row per lifecycle email actually sent
    to a tenant, keyed by a stable `email_key` (welcome, nudge_d3, nudge_d7,
    …, bonus_expiry, activated_celebration). The unique (tenant_id, email_key)
    constraint is the idempotency guard — each email is sent at most once per
    tenant, and a row is written only after a successful dispatch so a failed
    send is retried on a later sweep."""
    __tablename__ = "lifecycle_email_log"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    email_key = Column(String(40), nullable=False)
    sent_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "email_key", name="uq_lifecycle_email_tenant_key"),
    )


class RunSurfaceLink(Base):
    """embeddable-run-surface: a public, tokenized link that lets anyone run
    ONE pinned workflow version with no login, spending the owner's credits
    under hard caps. The token is the only credential and authorizes running
    exactly this version as the owner — never the graph, secrets, account, or
    other workflows/runs."""
    __tablename__ = "run_surface_links"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    workflow_version_id = Column(Integer, ForeignKey("workflow_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    token = Column(String(64), unique=True, index=True, nullable=False)
    label = Column(String(255), nullable=True)

    # Caps (NULL = unlimited for that dimension).
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    max_total_runs = Column(Integer, nullable=True)
    runs_used = Column(Integer, nullable=False, default=0, server_default="0")
    max_total_credits = Column(Integer, nullable=True)   # budget in credits
    credits_used = Column(Integer, nullable=False, default=0, server_default="0")
    rate_limit_per_hour = Column(Integer, nullable=True)       # per client IP

    # Allowlist of visitor-editable inputs/params with bounds (JSON). NULL =
    # "all top-level params with sane numeric bounds" (v1 default).
    exposed_inputs = Column(JSONB, nullable=True)
    password_hash = Column(String(255), nullable=True)
    frame_ancestors = Column(Text, nullable=False, default="*", server_default="*")

    revoked_at = Column(DateTime(timezone=True), nullable=True)
    visit_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RunSurfaceRun(Base):
    """embeddable-run-surface: maps each public run back to the link that
    spawned it. Scopes status/output reads to the originating token and powers
    the per-IP rate limit + usage counters."""
    __tablename__ = "run_surface_runs"

    id = Column(Integer, primary_key=True)
    link_id = Column(Integer, ForeignKey("run_surface_links.id", ondelete="CASCADE"), nullable=False, index=True)
    workflow_run_id = Column(Integer, ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    client_ip = Column(String(64), nullable=True)
    estimated_credits = Column(Integer, nullable=False, default=0, server_default="0")
    # Set once the reserved run-slot + credits have been given back to the
    # link (on run failure). Guards against double-refund.
    released = Column(Boolean, nullable=False, default=False, server_default="f")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class PromoCode(SoftDeleteMixin, Base):
    """
    Promotional codes for discounts and special offers.

    Allows platform admins to create discount codes that tenants can apply
    to their purchases or subscriptions. Use cases:
    - Launch promotions (e.g., "LAUNCH2024" for 20% off)
    - Partner referral codes
    - Customer retention offers
    - Seasonal campaigns

    Discount types:
    - percentage: Discount as % of total (e.g., 20% off)
    - fixed_amount: Fixed dollar discount (e.g., $10 off)
    - free_credits: Grant free credits (e.g., 1000 free credits)
    """
    __tablename__ = "promo_codes"

    id = Column(Integer, primary_key=True, index=True)

    # Promo code details
    code = Column(String(50), unique=True, nullable=False, index=True)  # "LAUNCH2024", "PARTNER50"
    description = Column(Text, nullable=True)  # Marketing description of the offer

    # Discount configuration
    discount_type = Column(String(20), nullable=False, index=True)  # "percentage", "fixed_amount", "free_credits"
    discount_value = Column(Integer, nullable=False)  # Meaning depends on type (%, cents, credits)

    # Usage limits
    max_uses = Column(Integer, nullable=True)  # Total uses allowed (null = unlimited)
    current_uses = Column(Integer, default=0, nullable=False)  # Current usage count
    max_uses_per_tenant = Column(Integer, default=1, nullable=False)  # Limit per tenant

    # Validity period
    valid_from = Column(DateTime(timezone=True), nullable=False, index=True)
    valid_until = Column(DateTime(timezone=True), nullable=True, index=True)  # null = no expiration

    # Status
    is_active = Column(Boolean, default=True, nullable=False, index=True)

    # Metadata
    created_by_email = Column(String(255), nullable=True)  # Who created this promo code
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    usages = relationship("PromoCodeUsage", back_populates="promo_code", cascade="all, delete-orphan")


class PromoCodeUsage(Base):
    """
    Tracks usage of promotional codes by tenants.

    Records each time a promo code is applied to ensure:
    - Per-tenant usage limits are enforced
    - Audit trail of all promo code redemptions
    - Revenue impact analysis
    """
    __tablename__ = "promo_code_usage"

    id = Column(Integer, primary_key=True, index=True)
    promo_code_id = Column(Integer, ForeignKey("promo_codes.id"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)

    # Usage details
    used_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    discount_applied_cents = Column(Integer, nullable=False)  # Actual discount amount in cents

    # Context
    applied_to_invoice_id = Column(Integer, nullable=True)  # Which invoice used this code
    applied_to_subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)  # Which subscription used this code

    # Metadata
    usage_metadata = Column(JSONB, nullable=True)  # Additional context (plan purchased, credits granted, etc.)

    # Relationships
    promo_code = relationship("PromoCode", back_populates="usages")
    tenant = relationship("Tenant")
    subscription = relationship("Subscription")

    # Constraint: Track usage per promo code and tenant
    __table_args__ = (
        UniqueConstraint("promo_code_id", "tenant_id", "used_at", name="uq_promo_code_usage_per_tenant"),
    )


class Currency(SoftDeleteMixin, Base):
    """
    Supported currencies and their exchange rates for multi-currency pricing display.

    USD is the base currency (rate = 1.0). All internal calculations are done in USD cents.
    Exchange rates are for display purposes only - actual billing always happens in USD.

    Exchange rates can be:
    - Manually configured by platform admins
    - Auto-updated via currency API integration (future enhancement)
    """
    __tablename__ = "currencies"

    id = Column(Integer, primary_key=True, index=True)

    # Currency identification
    code = Column(String(3), unique=True, nullable=False, index=True)  # ISO 4217 code (USD, EUR, GBP, etc.)
    name = Column(String(100), nullable=False)  # Full name (US Dollar, Euro, etc.)
    symbol = Column(String(10), nullable=False)  # Currency symbol ($, €, £, etc.)

    # Exchange rate (relative to USD)
    # Example: If 1 USD = 0.85 EUR, then EUR rate = 0.85
    #          If 1 USD = 1.25 CAD, then CAD rate = 1.25
    exchange_rate = Column(Integer, nullable=False)  # Stored as basis points (10000 = 1.0)

    # Status and configuration
    is_active = Column(Boolean, default=True, nullable=False)  # Whether currency is available for display
    is_default = Column(Boolean, default=False, nullable=False)  # Default currency for new users

    # Rate update tracking
    rate_updated_at = Column(DateTime(timezone=True), nullable=True)
    rate_updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    rate_source = Column(String(100), nullable=True)  # "manual", "api:exchangerate-api.io", etc.

    # Display configuration
    decimal_places = Column(Integer, default=2, nullable=False)  # Number of decimal places to show
    display_order = Column(Integer, default=0, nullable=False)  # Order in currency selector

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    rate_updated_by = relationship("User", foreign_keys=[rate_updated_by_user_id])


class PricingAuditLog(Base):
    """
    Audit trail for all pricing configuration changes.

    Tracks who changed pricing, what changed, and when for compliance and analysis.
    """
    __tablename__ = "pricing_audit_log"

    id = Column(Integer, primary_key=True, index=True)
    changed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # What changed
    old_postpaid_price_cents = Column(Integer, nullable=True)
    new_postpaid_price_cents = Column(Integer, nullable=True)
    old_overage_price_cents = Column(Integer, nullable=True)
    new_overage_price_cents = Column(Integer, nullable=True)

    # Context
    change_reason = Column(Text, nullable=True)  # Optional reason for the change
    change_metadata = Column(JSONB, nullable=True)  # Additional context (e.g., affected tenant count, scheduled_for date)

    # Timestamps
    changed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    effective_at = Column(DateTime(timezone=True), nullable=True, index=True)  # When price takes effect (for scheduled changes)

    # Relationships
    changed_by = relationship("User")


class ProposalStatus(str, enum.Enum):
    """Status of a pricing change proposal."""
    PENDING = "pending"          # Awaiting approvals
    APPROVED = "approved"        # Sufficient approvals, ready to apply
    REJECTED = "rejected"        # Rejected by an admin
    APPLIED = "applied"          # Successfully applied to billing settings
    CANCELLED = "cancelled"      # Cancelled by proposer
    EXPIRED = "expired"          # Expired before receiving approvals


class PricingChangeProposal(Base):
    """
    Pricing change proposals requiring multi-admin approval.

    Enforces governance by requiring multiple platform admins to approve
    pricing changes before they take effect. Configurable approval threshold
    (e.g., require 2 out of 3 admins to approve).

    Workflow:
    1. Admin creates proposal with proposed pricing changes
    2. Other admins review and approve/reject
    3. Once approval threshold is met, proposal is marked as approved
    4. Proposal can be applied immediately or scheduled for future
    5. After application, creates PricingAuditLog entry
    """
    __tablename__ = "pricing_change_proposals"

    id = Column(Integer, primary_key=True, index=True)

    # Proposal creator
    proposed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Proposed changes
    new_postpaid_price_cents = Column(Integer, nullable=True)
    new_overage_price_cents = Column(Integer, nullable=True)

    # Proposal details
    title = Column(String(200), nullable=False)  # "Q4 2024 Pricing Adjustment"
    justification = Column(Text, nullable=True)  # Why this change is needed
    impact_analysis = Column(JSONB, nullable=True)  # Affected tenants, revenue impact, etc.

    # Approval requirements
    required_approvals = Column(Integer, default=2, nullable=False)  # How many approvals needed
    current_approvals = Column(Integer, default=0, nullable=False)  # Current approval count

    # Status tracking
    status = Column(Enum(ProposalStatus), default=ProposalStatus.PENDING, nullable=False, index=True)

    # When to apply (null = apply immediately after approval)
    scheduled_for = Column(DateTime(timezone=True), nullable=True, index=True)

    # Application tracking
    applied_at = Column(DateTime(timezone=True), nullable=True)
    applied_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    audit_log_id = Column(Integer, ForeignKey("pricing_audit_log.id"), nullable=True)  # Link to audit log when applied

    # Rejection tracking
    rejected_at = Column(DateTime(timezone=True), nullable=True)
    rejected_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    rejection_reason = Column(Text, nullable=True)

    # Expiration
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)  # Auto-expire if not approved by this time

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    proposed_by = relationship("User", foreign_keys=[proposed_by_user_id])
    applied_by = relationship("User", foreign_keys=[applied_by_user_id])
    rejected_by = relationship("User", foreign_keys=[rejected_by_user_id])
    approvals = relationship("PricingProposalApproval", back_populates="proposal", cascade="all, delete-orphan")
    audit_log = relationship("PricingAuditLog", foreign_keys=[audit_log_id])


class PricingProposalApproval(Base):
    """
    Individual approvals for pricing change proposals.

    Tracks which admins have approved/rejected each proposal,
    when they did so, and their comments.
    """
    __tablename__ = "pricing_proposal_approvals"

    id = Column(Integer, primary_key=True, index=True)
    proposal_id = Column(Integer, ForeignKey("pricing_change_proposals.id"), nullable=False, index=True)
    admin_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Approval decision
    approved = Column(Boolean, nullable=False)  # True = approved, False = rejected
    comments = Column(Text, nullable=True)  # Admin's comments on their decision

    # Timestamps
    decided_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    proposal = relationship("PricingChangeProposal", back_populates="approvals")
    admin = relationship("User")

    # Constraint: Each admin can only approve/reject a proposal once
    __table_args__ = (
        UniqueConstraint("proposal_id", "admin_user_id", name="uq_proposal_admin_approval"),
    )



class ResourceLock(Base):
    __tablename__ = "resource_locks"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), index=True)
    user_id = Column(ForeignKey("users.id"), index=True)
    target_type = Column(String(50), index=True)
    target_id = Column(Integer, index=True)
    expires_at = Column(DateTime(timezone=True), index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Optional free-text note the holder attaches when locking ("editing hero
    # shot", "do not touch — client review"). Surfaced in the lock UI so other
    # users know WHY a resource is held. Nullable; inert on legacy rows.
    reason = Column(Text, nullable=True)
    # Last time the holder renewed/extended the lock (push-out of expires_at).
    # NULL until the first renew. Lets the UI show "renewed 2m ago".
    renewed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User")
    tenant = relationship("Tenant")

    __table_args__ = (
        UniqueConstraint("target_type", "target_id", name="uq_active_lock_per_resource"),
    )

    @property
    def locked_by_username(self):
        """Display name of the holder, for the lock badge. Safe while the
        owning Session is open (the router serializes before commit/close)."""
        return getattr(self.user, "username", None) if self.user else None

    @property
    def locked_by_email(self):
        return getattr(self.user, "email", None) if self.user else None


class WebhookEndpoint(SoftDeleteMixin, Base):
    __tablename__ = "webhook_endpoints"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    url = Column(String(2048), nullable=False)
    description = Column(Text)
    signing_secret = Column(String(255), nullable=False, unique=True)
    subscribed_events = Column(SafeARRAY)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    tenant = relationship("Tenant")


class RunWebhookSubscription(SoftDeleteMixin, Base):
    """GX.R3 — per-workflow run-completion webhook subscription.

    An external app registers a URL that MOD POSTs a signed payload to when a
    run of this workflow reaches a terminal state (completed/failed/cancelled).
    Tenant-scoped, soft-delete (ISO 27001: no hard deletes). The HMAC signing
    secret is generated server-side, encrypted with the tenant's encryption key
    (same pattern as secret values in crud/secret_cud.py) and shown to the
    caller exactly once at creation / on rotation.
    """
    __tablename__ = "run_webhook_subscriptions"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=False, index=True)
    url = Column(String(2048), nullable=False)
    # Tenant-key-encrypted HMAC signing secret (shown once, never re-readable).
    secret_encrypted = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True, server_default="true", index=True)
    description = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    workflow = relationship("Workflow")


class RunWebhookDelivery(Base):
    """GX.R3 — append-only delivery log for run-completion webhooks.

    One row per (target, run terminal transition). `status` is the CURRENT
    delivery state (pending/delivered/failed/rejected) and `attempts` counts
    sends; the per-attempt detail (timestamp, http status, error) lives in
    RunWebhookDeliveryAttempt, which is strictly append-only — ISO 27001: no
    updates or deletes to the attempt rows. No response bodies are stored
    (they may carry secrets).
    """
    __tablename__ = "run_webhook_deliveries"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    workflow_id = Column(Integer, ForeignKey("workflows.id"), index=True)
    # NULL for per-run callback_url deliveries (no subscription row).
    subscription_id = Column(Integer, ForeignKey("run_webhook_subscriptions.id", ondelete="SET NULL"), nullable=True, index=True)
    run_id = Column(Integer, ForeignKey("workflow_runs.id"), index=True)
    url = Column(String(2048), nullable=False)
    event = Column(String(50), nullable=False, index=True)
    payload_body = Column(Text, nullable=False)  # exact signed JSON bytes (serialized deterministically)
    # X-MOD-Delivery header value (uuid4), stable per delivery.
    delivery_uuid = Column(String(36), nullable=False, unique=True)
    # One-shot signing secret (encrypted) for per-run callback rows only;
    # subscription rows decrypt the subscription's stored secret instead.
    secret_encrypted = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_attempted_at = Column(DateTime(timezone=True), nullable=True)
    last_http_status = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    attempts_detail = relationship("RunWebhookDeliveryAttempt", back_populates="delivery")


class RunWebhookDeliveryAttempt(Base):
    """GX.R3 — append-only per-attempt record (ISO 27001: never updated).

    Records status at send time, the HTTP status (if any), and a redacted
    error (exception type + short message — never request/response bodies,
    which may carry secrets).
    """
    __tablename__ = "run_webhook_delivery_attempts"
    id = Column(Integer, primary_key=True)
    delivery_id = Column(Integer, ForeignKey("run_webhook_deliveries.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_number = Column(Integer, nullable=False)
    outcome = Column(String(20), nullable=False)  # "sent_2xx" | "sent_non2xx" | "error" | "rejected"
    http_status = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)  # redacted: exception type + short message, no bodies
    attempted_at = Column(DateTime(timezone=True), server_default=func.now())

    delivery = relationship("RunWebhookDelivery", back_populates="attempts_detail")


class WorkflowTriggerRule(SoftDeleteMixin, Base):
    __tablename__ = "workflow_trigger_rules"

    id = Column(Integer, primary_key=True, index=True)
    workflow_version_id = Column(Integer, ForeignKey("workflow_versions.id"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"),
                       nullable=False)  # Assuming a 'tenants' table or that 'tenant_id' maps to a user's tenant

    # Defines the broad category of the trigger (e.g., 'FILE_UPLOAD', 'PLACEHOLDER_FILLED', 'METADATA_UPDATED')
    trigger_type = Column(String, nullable=False)

    # A flexible JSONB column to store specific trigger conditions as key-value pairs
    conditions = Column(JSONB, nullable=False, default={})

    is_active = Column(Boolean, default=True)  # To enable/disable the rule
    priority = Column(Integer,
                      default=0)  # Lower number means higher priority for rule evaluation in case of multiple matches

    # Relationships
    workflow_version = relationship("WorkflowVersion", back_populates="trigger_rules")
    # If you have a Tenant model:
    # tenant = relationship("Tenant")


class File(Base, SoftDeleteMixin):
    """
    File records for workflow compatibility.
    Uses soft-delete for CMMC/ISO compliance - records are never permanently deleted.

    NOTE: No unique constraint on storage_path because with content-addressed storage,
    multiple File records can legitimately point to the same storage_path (same content
    uploaded to different folders, or re-uploaded after deletion).
    ContentBlob handles deduplication at the storage layer.
    """
    __tablename__ = "files"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    cog_id = Column(Integer, ForeignKey("cogs.id"), nullable=True, index=True)  # Folder the file belongs to
    name = Column(String, nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    mime_type = Column(String, nullable=False)
    storage_path = Column(String, nullable=False, index=True)  # Non-unique index for lookups
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tenant = relationship("Tenant")
    user = relationship("User")
    cog = relationship("Cog")


class DownloadJobStatus(str, enum.Enum):
    """Status of a download job."""
    PENDING = "pending"
    PREPARING = "preparing"
    DOWNLOADING = "downloading"
    CREATING_ZIP = "creating_zip"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"


class DownloadJob(Base):
    """
    Tracks the progress of folder/multi-file download jobs.
    Used to provide real-time progress updates to users during zip creation.
    """
    __tablename__ = "download_jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Job details
    status = Column(Enum(DownloadJobStatus), default=DownloadJobStatus.PENDING, nullable=False)
    total_files = Column(Integer, default=0)
    processed_files = Column(Integer, default=0)
    current_file = Column(String, nullable=True)  # Name of file currently being processed

    # Orchestrator run tracking — links this download job to the
    # WorkflowRun.orchestrator_run_id that produced it.
    orchestrator_run_id = Column(String, nullable=True)

    # Result (when completed)
    download_url = Column(String, nullable=True)
    filename = Column(String, nullable=True)
    error_message = Column(String, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)  # When download URL expires

    tenant = relationship("Tenant")
    user = relationship("User")


# =============================================================================
# --- Wizard Queue (wizard-queue-redesign) ---
# =============================================================================
#
# One unified table every long-running wizard writes to — the single
# source of truth for both the live activity console and persisted
# history (design D7). Purely observational: it does NOT throttle or
# order execution (D12); each wizard keeps its own dispatch path.
# Rows are soft-delete only (ISO: NO hard delete) and a BEFORE DELETE /
# TRUNCATE trigger on wizard_runs (installed by the migration) enforces
# that at the DB level while still allowing UPDATE for status
# transitions.


class WizardRunType(str, enum.Enum):
    """Which wizard produced the run. Kebab-case values are the wire
    contract shared with the frontends (D2/D13)."""
    NODE = "node"
    STACK = "stack"
    AGENT_PROJECT = "agent-project"
    BUILD_REPAIR = "build-repair"
    CONTAINER_ADAPTER = "container-adapter"


class WizardRunStatus(str, enum.Enum):
    """Canonical status lifecycle every wizard maps its native states
    onto (D13). Terminal states are exactly succeeded / failed /
    canceled."""
    QUEUED = "queued"
    WAITING_FOR_WORKER = "waiting-for-worker"
    RUNNING = "running"
    VERIFYING = "verifying"
    # conversational-project-builder §3.3 — a paused, non-failing pause on a
    # checkpoint hand-off: the build stopped at a meaningful unit and is
    # waiting for the human to test + give feedback. Durable and resumable
    # (survives a manager restart); NOT terminal, so the reaper never fails a
    # project that is merely waiting on its user.
    WAITING_FOR_USER = "waiting-for-user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


# Terminal states — a run in one of these is finished and cancelable no
# longer; only these are retryable (failed / canceled).
WIZARD_TERMINAL_STATUSES = frozenset({
    WizardRunStatus.SUCCEEDED,
    WizardRunStatus.FAILED,
    WizardRunStatus.CANCELED,
})
WIZARD_RETRYABLE_STATUSES = frozenset({
    WizardRunStatus.FAILED,
    WizardRunStatus.CANCELED,
})


class WizardRunOrigin(str, enum.Enum):
    """Provenance for the platform-level classification (D17). A run is
    platform-level if it is a SYSTEM process OR was created by a
    PLATFORM ADMIN; otherwise it is a tenant user's run."""
    SYSTEM = "system"
    PLATFORM_ADMIN = "platform-admin"
    TENANT_USER = "tenant-user"


class WizardRun(SoftDeleteWithActorMixin, Base):
    """A single long-running wizard run surfaced in the Wizard Queue."""
    __tablename__ = "wizard_runs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # --- What ----------------------------------------------------------
    wizard_type = Column(
        Enum(WizardRunType, name="wizard_run_type_enum"),
        nullable=False, index=True,
    )
    target_name = Column(String(500), nullable=True)
    # Artifact back-link ("open produced artifact"): a type + id pair so
    # the row can deep-link to the produced node / stack / workflow.
    target_ref_type = Column(String(50), nullable=True)   # node|stack|workflow|...
    target_ref_id = Column(String(200), nullable=True)

    # --- Lifecycle -----------------------------------------------------
    status = Column(
        Enum(WizardRunStatus, name="wizard_run_status_enum"),
        nullable=False, default=WizardRunStatus.QUEUED, index=True,
    )
    progress = Column(Integer, nullable=False, default=0)   # 0..100 percent

    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Float, nullable=True)
    error_message = Column(Text, nullable=True)

    # --- Scope / classification (D5/D17) -------------------------------
    owner_user_id = Column(ForeignKey("users.id"), nullable=True, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=True, index=True)
    is_platform_level = Column(
        Boolean, nullable=False, default=False,
        server_default="false", index=True,
    )
    origin = Column(
        Enum(WizardRunOrigin, name="wizard_run_origin_enum"),
        nullable=False, default=WizardRunOrigin.TENANT_USER,
    )

    # --- Linking -------------------------------------------------------
    # retry_of: a retry is a NEW run linked back to the original (D14).
    retry_of_id = Column(ForeignKey("wizard_runs.id"), nullable=True, index=True)
    # parent_run_id: composite runs (agent/project) spawn child node
    # builds — set so a HARD cancel can cascade to children (D21).
    parent_run_id = Column(ForeignKey("wizard_runs.id"), nullable=True, index=True)

    # --- Native execution bridge --------------------------------------
    # Links the surfaced run back to the wizard's own execution record so
    # the cancel hook can kill the real task and status can be reconciled.
    # No backfill: only new runs write these (D16).
    native_source = Column(String(50), nullable=True)   # node_build_job|api_task_queue|project_session|...
    native_ref = Column(String(200), nullable=True, index=True)

    # --- Log source descriptor (D3) -----------------------------------
    log_source = Column(String(50), nullable=True)   # llm_step|container_build|...

    extra = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    # --- Relationships -------------------------------------------------
    owner = relationship("User", foreign_keys=[owner_user_id])
    tenant = relationship("Tenant")
    retry_of = relationship(
        "WizardRun", remote_side=[id], foreign_keys=[retry_of_id],
    )
    parent_run = relationship(
        "WizardRun", remote_side=[id], foreign_keys=[parent_run_id],
        backref="child_runs",
    )
    logs = relationship(
        "WizardRunLog", back_populates="run",
        cascade="all, delete-orphan",
        order_by="WizardRunLog.seq",
    )


class WizardRunLog(Base):
    """A single persisted log line for a wizard run.

    Logs are stored under the system-log ROTATING retention policy
    (D20) — a bounded window that a retention job trims — so this table
    is deliberately NOT append-only-forever and carries NO delete-
    blocking trigger (rotation must be able to physically remove old
    rows). Every line is already secret-redacted before it is written
    (D19), so the persisted store, the live stream, and exports can
    never leak a secret.
    """
    __tablename__ = "wizard_run_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(
        ForeignKey("wizard_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    seq = Column(Integer, nullable=False, default=0)
    level = Column(String(16), nullable=True)   # info|warn|error
    message = Column(Text, nullable=False)      # ALWAYS pre-redacted
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    # FX.57 (Q40=A) — set by the nightly system-log archive job after the
    # gzip JSONL export is verified in the archive bucket. NULL = still in
    # the online searchable window. Never deleted; rows stay soft-marked.
    archived_at = Column(DateTime(timezone=True), nullable=True, index=True)

    run = relationship("WizardRun", back_populates="logs")


class TenantEmailConfig(Base):
    __tablename__ = "tenant_email_configs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, unique=True)

    # Provider type: 'brevo', 'mailgun', 'custom' (SMTP), or 'sendgrid' (legacy)
    # Default to mailgun for localhost compatibility (Brevo doesn't work with localhost)
    provider = Column(String, nullable=True, default='mailgun')

    # Common fields
    sender_name = Column(String, nullable=True)
    sender_email = Column(String, nullable=True)

    # Custom SMTP fields (used when provider='custom')
    # SMTP credentials stored here since they're configuration, not secrets
    smtp_host = Column(String, nullable=True)
    smtp_port = Column(Integer, nullable=True)
    smtp_user = Column(String, nullable=True)
    encrypted_smtp_password = Column(LargeBinary, nullable=True)
    # Explicit transport encryption: NULL / 'auto' = guess from the port (465 ssl,
    # 25 none, else starttls: the historical rule); 'starttls' | 'ssl' | 'none'
    # = explicit (utils/smtp_security.py is the single rule). Migration 20261109b.
    smtp_security = Column(String(16), nullable=True)
    # TRUE = encrypt but skip certificate + hostname verification (private-CA /
    # self-signed internal relay). Default FALSE = full verification.
    smtp_allow_selfsigned = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    # launch-day-followup-5 §1A — OpenBao-backed SMTP password.
    # When set, mailer reads the password from OpenBao at this KV v2 path
    # (e.g. "platform/email-credentials"). When NULL, fall back to the
    # legacy encrypted_smtp_password / tenant-secret resolution.
    password_vault_path = Column(String(255), nullable=True)

    # launch-day-followup-5 §1A — test-send result tracking. Updated by
    # POST /admin/email-config/test so the platform admin can see the
    # last test outcome on page load without re-sending.
    last_tested_at = Column(DateTime(timezone=True), nullable=True)
    last_test_result = Column(String(32), nullable=True)  # 'success' / 'failed' / NULL
    last_test_error = Column(Text, nullable=True)

    # Brevo and Mailgun credentials stored in Secrets table
    # No fields needed here - secrets are: brevo_api_key, brevo_template_id, mailgun_api_key, mailgun_domain

    # Legacy SendGrid fields (kept for backward compatibility)
    email_from = Column(String, nullable=True)
    encrypted_sendgrid_api_key = Column(String, nullable=True)

    tenant = relationship("Tenant", back_populates="email_config")


class SMTPSettings(SoftDeleteMixin, Base):
    __tablename__ = "smtp_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, index=True)
    smtp_server = Column(String, index=True)
    smtp_port = Column(Integer)
    smtp_username = Column(String)
    encrypted_smtp_password = Column(LargeBinary)

    owner = relationship("User", back_populates="smtp_settings")


class AuditAction(str, enum.Enum):
    """Audit log action types for permission and security events.

    CMMC/ISO 27001 Compliance: AU-2 (Audit Events)
    TPN (Trusted Partner Network) Compliance: CS-5 (Logging & Monitoring)
    """
    # Permission events
    PERMISSION_GRANTED = "permission_granted"
    PERMISSION_REVOKED = "permission_revoked"
    PERMISSION_UPDATED = "permission_updated"
    PERMISSION_EXPIRED = "permission_expired"
    PERMISSION_REQUESTED = "permission_requested"
    PERMISSION_REQUEST_APPROVED = "permission_request_approved"
    PERMISSION_REQUEST_DENIED = "permission_request_denied"
    PERMISSION_REQUEST_CANCELLED = "permission_request_cancelled"

    # Security events (CMMC AU-2 required)
    AUTH_LOGIN_SUCCESS = "auth_login_success"
    AUTH_LOGIN_FAILED = "auth_login_failed"
    AUTH_TOKEN_INVALID = "auth_token_invalid"
    AUTH_TOKEN_EXPIRED = "auth_token_expired"
    AUTH_API_KEY_INVALID = "auth_api_key_invalid"
    AUTH_API_KEY_CREATED = "auth_api_key_created"
    AUTH_API_KEY_REVOKED = "auth_api_key_revoked"
    AUTH_IMPERSONATION = "auth_impersonation"
    AUTH_PRIVILEGE_ESCALATION = "auth_privilege_escalation"
    WEBHOOK_SIGNATURE_FAILED = "webhook_signature_failed"
    SECURITY_CONFIG_ERROR = "security_config_error"

    # GX.R3 — run-completion webhook lifecycle (subscription create/delete +
    # secret rotation). Secret VALUES are never written to the audit row.
    RUN_WEBHOOK_SUBSCRIPTION_CREATED = "run_webhook_subscription_created"
    RUN_WEBHOOK_SUBSCRIPTION_DELETED = "run_webhook_subscription_deleted"
    RUN_WEBHOOK_SECRET_ROTATED = "run_webhook_secret_rotated"

    # FX.117 (Q104=A) — one row per secret USE at run-time injection: the
    # worker run-token path (GET /secrets/by-name/{name}/value) fetched a
    # value for a workflow run. Metadata carries who/what (user, tenant,
    # optional workflow_run_id + node_id) and the secret id/name — NEVER
    # the value.
    SECRET_INJECTED = "secret_injected"

    # FX.117 p3b (per-secret allowed-workflows scope) — the per-secret
    # allowed-workflow scope list was replaced (rows soft-deleted / inserted
    # in services/secret_workflow_scope_service.py). Metadata carries the
    # secret id/name, the workflow ids added/removed and the acting user —
    # NEVER a secret value.
    SECRET_SCOPE_UPDATED = "secret_scope_updated"

    # FX.117 p4a — a per-secret expiry was SET (a live `secret_expiries` row
    # inserted/moved) or CLEARED (its live row soft-deleted) via
    # PUT /secrets/{id}/expiry. Metadata carries the secret id/name, the new
    # expires_at (or null when cleared) and the acting user — NEVER a secret
    # value.
    SECRET_EXPIRY_SET = "secret_expiry_set"

    # FX.117 p4b — the expiry-reminder job (services/scheduler.py
    # fx117_secret_expiry_reminders) notified the tenant admins that a secret
    # is expiring or already expired. Metadata carries the secret id/name, the
    # expires_at, days_left and whether it is already past expiry — NEVER a
    # secret value.
    SECRET_EXPIRY_REMINDER_SENT = "secret_expiry_reminder_sent"

    # FX.131 (Q118=A) — a trained-model artifact (customer LoRA) was
    # downloaded / a presigned URL was issued for it. Metadata carries the
    # tenant, artifact id + name and the object key — never weights content.
    TRAINED_MODEL_DOWNLOADED = "trained_model_downloaded"

    # FX.90 (Q76=A) — a tenant file upload was blocked because clamd
    # reported it as infected. Metadata carries the tenant, uploader,
    # filename, size and clamd signature — never file content.
    MALWARE_UPLOAD_BLOCKED = "malware_upload_blocked"

    # FX.90 (Q76=A) — a scan ERROR (clamd unreachable/timeout/size cap)
    # occurred while malware scanning was enabled; metadata carries the
    # tenant, uploader, filename, error detail and the fail mode that was
    # applied (upload refused with 503, or accepted under fail-open).
    MALWARE_SCAN_ERROR = "malware_scan_error"

    # FX.101 part 1 (owner decision Q87=A) — a request was refused
    # (403) because the caller's IP is outside the tenant's IP
    # allowlist. Metadata carries the tenant, user and client IP —
    # never credentials or tokens.
    TENANT_IP_BLOCKED = "tenant_ip_blocked"

    # FX.101 part 1 (owner decision Q87=A) — the tenant's IP allowlist
    # was updated via PUT /v1/tenant/security/ip-allowlist (CIDRs
    # validated; lockout-safe). Metadata carries the new entry count.
    TENANT_IP_ALLOWLIST_UPDATED = "tenant_ip_allowlist_updated"

    # Platform admin actions (CMMC AC-5 / AU-2 - admin accountability)
    ADMIN_TENANT_VIEW = "admin_tenant_view"
    ADMIN_TENANT_MODIFY = "admin_tenant_modify"
    ADMIN_TENANT_DELETE = "admin_tenant_delete"
    ADMIN_USER_VIEW = "admin_user_view"
    ADMIN_USER_MODIFY = "admin_user_modify"
    ADMIN_USER_DEACTIVATE = "admin_user_deactivate"
    ADMIN_USER_SUSPEND = "admin_user_suspend"
    ADMIN_USER_DELETE = "admin_user_delete"
    ADMIN_BILLING_VIEW = "admin_billing_view"
    ADMIN_BILLING_MODIFY = "admin_billing_modify"
    ADMIN_CREDITS_ADJUST = "admin_credits_adjust"
    ADMIN_PLAN_CREATE = "admin_plan_create"
    ADMIN_PLAN_MODIFY = "admin_plan_modify"
    ADMIN_PLAN_DELETE = "admin_plan_delete"
    ADMIN_WORKER_VIEW = "admin_worker_view"
    ADMIN_WORKER_MODIFY = "admin_worker_modify"
    ADMIN_WORKER_ASSIGN = "admin_worker_assign"
    ADMIN_CONFIG_CHANGE = "admin_config_change"
    ADMIN_DATA_EXPORT = "admin_data_export"
    ADMIN_CROSS_TENANT_ACCESS = "admin_cross_tenant_access"
    # ISO 27001 / SOC 2: emitted when a platform admin attempts to
    # access tenant data (cogs, etc.) belonging to a tenant other than
    # their own and is BLOCKED by the data-isolation contract. Distinct
    # from ADMIN_CROSS_TENANT_ACCESS (which logs ALLOWED platform-admin
    # actions on documented cross-tenant endpoints like /users/tenants).
    # Debounced 5-min per (actor, target_tenant) via Redis.
    ADMIN_CROSS_TENANT_DATA_ACCESS_BLOCKED = "admin_cross_tenant_data_access_blocked"
    ADMIN_SECURITY_OVERRIDE = "admin_security_override"
    ADMIN_AUDIT_LOG_VIEW = "admin_audit_log_view"
    ADMIN_SYSTEM_MAINTENANCE = "admin_system_maintenance"
    ADMIN_FEATURE_FLAG_CHANGE = "admin_feature_flag_change"
    # GY.C1 — a settings write that would WEAKEN a value an active
    # compliance profile requires was refused (HTTP 409). Metadata holds
    # the setting key, the attempted value, the blocking profile and the
    # requirement (setting values only — never secrets).
    COMPLIANCE_PROFILE_LOCK_REJECTED = "compliance_profile_lock_rejected"
    # GY.C0 — the admin confirmed a compliance-profile PREVIEW and the
    # apply flow wrote the previewed settings + the profile list
    # (one row per changed setting, metadata: key/old/new/disruptive/
    # profiles/grace_deadline, plus a summary row with applied keys,
    # disruptive_count and the grace window).
    COMPLIANCE_PROFILE_APPLIED = "compliance_profile_applied"

    # tenant-resource-monitor — leak detection + admin-config-change
    # surface area. RESOURCE_LEAK_DETECTED is debounced 24h per
    # (tenant_id, user_id) so admins get one nudge per offender per
    # window. ADMIN_RESOURCE_MONITOR_SETTINGS_UPDATED fires on platform-
    # admin writes of cost rates / threshold / enabled flag.
    RESOURCE_LEAK_DETECTED = "resource_leak_detected"
    ADMIN_RESOURCE_MONITOR_SETTINGS_UPDATED = "admin_resource_monitor_settings_updated"

    # harbor-scan-followup-node — fires when the recurring system
    # workflow's container retriggers Harbor's manual /scan endpoint
    # for an artifact stuck in Pending / Running past the threshold.
    # One row per artifact retriggered; skipped (fresh / already
    # terminal) artifacts produce no event. Lets operators answer
    # "was this image's scan retriggered, and when?" via the audit log.
    HARBOR_SCAN_RETRIGGERED = "harbor_scan_retriggered"

    # GY.C14 — update.sh records each applied platform update (from->to
    # versions, per-service image digests, operator, start/finish time) as
    # one security-audit row via audit.log_security_event.
    PLATFORM_UPDATE_APPLIED = "platform_update_applied"

    # GSEC Q14 — the startup `down -v` guard (CORE/startup/lib/volume-guard.sh)
    # records EVERY attempted volume-deleting shutdown (allowed or refused) as
    # one security-audit row via scripts/record_volume_guard.
    VOLUME_DELETE_GUARD = "volume_delete_guard"

    # FX.42 (Q24=A) — data-subject request (DSAR) tracker lifecycle
    # (services/dsar_tracker_service.py): one row per create /
    # acknowledge / extend / close transition. Previously these reused
    # ADMIN_DATA_EXPORT / ADMIN_CONFIG_CHANGE / INCIDENT_RESOLVED, which
    # auditors misread; the row metadata still carries dsar_request_id +
    # dsar_transition.
    DSAR_REQUEST_CREATED = "dsar_request_created"
    DSAR_REQUEST_ACKNOWLEDGED = "dsar_request_acknowledged"
    DSAR_REQUEST_EXTENDED = "dsar_request_extended"
    DSAR_REQUEST_CLOSED = "dsar_request_closed"

    # FX.114 (owner Q101=A) — service accounts + API key lifecycle
    # (services/service_accounts.py): tenant-admin CRUD on the
    # non-human principals that own API keys, rotation, and the
    # admin one-click revoke. Never hard-deleted; the row metadata
    # carries service_account_id / api_key_id + the state transition.
    SERVICE_ACCOUNT_CREATED = "service_account_created"
    SERVICE_ACCOUNT_UPDATED = "service_account_updated"
    SERVICE_ACCOUNT_DISABLED = "service_account_disabled"
    SERVICE_ACCOUNT_REASSIGNED = "service_account_reassigned"
    API_KEY_ROTATED = "api_key_rotated"
    API_KEY_ADMIN_REVOKED = "api_key_admin_revoked"
    API_KEY_AUTO_DISABLED = "api_key_auto_disabled"

    # FX.126 (owner Q113=A) — platform asset inventory
    # (services/asset_inventory.py). ASSET_WORKER_ENROLLMENT_ALERT:
    # enrollment attempt from an unknown/unapproved device (bad/expired
    # token, name-pattern mismatch, or a new PENDING enrollment) —
    # rate-limited per worker_name. ASSET_WORKER_OUTDATED_ALERT: daily
    # scheduler tick flags a worker whose reported agent_version is
    # older than the current release manifest. ASSET_WORKER_METADATA_
    # UPDATED: audited PATCH of owner_name / data_class / site_region.
    ASSET_WORKER_ENROLLMENT_ALERT = "asset_worker_enrollment_alert"
    ASSET_WORKER_OUTDATED_ALERT = "asset_worker_outdated_alert"
    ASSET_WORKER_METADATA_UPDATED = "asset_worker_metadata_updated"

    # ==========================================================================
    # TPN (Trusted Partner Network) Content Security Events
    # MPA Content Security Best Practices v4.0+ Compliance
    # ==========================================================================

    # CS-7: Content Transfer Security
    # Track all content downloads, uploads, and external transfers
    CONTENT_DOWNLOAD = "content_download"              # User downloaded content
    CONTENT_DOWNLOAD_PRESIGNED = "content_download_presigned"  # Presigned URL generated
    CONTENT_UPLOAD = "content_upload"                  # User uploaded content
    CONTENT_TRANSFER_EXTERNAL = "content_transfer_external"    # Content sent externally
    CONTENT_TRANSFER_INTERNAL = "content_transfer_internal"    # Content moved internally
    CONTENT_SHARE_LINK_CREATED = "content_share_link_created"  # Share link generated
    CONTENT_SHARE_LINK_ACCESSED = "content_share_link_accessed"  # Share link used
    CONTENT_SHARE_LINK_REVOKED = "content_share_link_revoked"  # Share link disabled
    CONTENT_RENAMED = "content_renamed"                # P3 — file/folder renamed
    CONTENT_MOVED = "content_moved"                    # P3 — file/folder reparented
    SUPPORT_CHAT_MESSAGE = "support_chat_message"      # P4 — user message in support chat
    SUPPORT_CHAT_EMAILED = "support_chat_emailed"      # P4 — conversation emailed

    # CS-8: Content Destruction / Secure Deletion
    # Track content lifecycle end with verification
    CONTENT_DELETE_INITIATED = "content_delete_initiated"      # Deletion requested
    CONTENT_DELETE_COMPLETED = "content_delete_completed"      # Deletion confirmed
    CONTENT_DELETE_VERIFIED = "content_delete_verified"        # Secure wipe verified
    CONTENT_DELETE_CERTIFICATE = "content_delete_certificate"  # Destruction cert issued
    CONTENT_RETENTION_EXPIRED = "content_retention_expired"    # Auto-delete triggered
    CONTENT_PURGE_REQUESTED = "content_purge_requested"        # Full purge requested
    CONTENT_PURGE_COMPLETED = "content_purge_completed"        # Full purge done

    # CS-1: Asset Management / Content Tracking
    # Track content throughout its lifecycle
    CONTENT_INGESTED = "content_ingested"              # New content received
    CONTENT_CLASSIFIED = "content_classified"          # Security classification set
    CONTENT_RECLASSIFIED = "content_reclassified"      # Classification changed
    CONTENT_WATERMARKED = "content_watermarked"        # Watermark applied
    CONTENT_WATERMARK_DETECTED = "content_watermark_detected"  # Watermark found in file
    CONTENT_FINGERPRINTED = "content_fingerprinted"    # Content hash/fingerprint stored

    # CS-6: Incident Response
    # Track security incidents and responses
    INCIDENT_DETECTED = "incident_detected"            # Potential breach detected
    INCIDENT_REPORTED = "incident_reported"            # Incident reported to stakeholders
    INCIDENT_ESCALATED = "incident_escalated"          # Escalated to higher authority
    INCIDENT_RESOLVED = "incident_resolved"            # Incident closed
    SUSPICIOUS_ACTIVITY = "suspicious_activity"        # Unusual behavior flagged

    # Content Access Anomalies (TPN CS-5)
    ACCESS_UNUSUAL_VOLUME = "access_unusual_volume"    # Bulk download detected
    ACCESS_UNUSUAL_TIME = "access_unusual_time"        # Off-hours access
    ACCESS_UNUSUAL_LOCATION = "access_unusual_location"  # Unusual IP/geo
    ACCESS_DENIED_REPEATED = "access_denied_repeated"  # Multiple failed attempts

    # add-self-service-sso — Phase A (IdP lifecycle), Phase B (domain
    # verification), Phase C (login UX), Phase D (account linking),
    # Phase E (JIT provisioning). Every SSO-touching codepath emits
    # one of these to PermissionAuditLog so the Admin → SSO → Activity
    # page has a single source of truth for the per-tenant audit trail.
    SSO_IDP_CREATED = "sso_idp_created"
    SSO_IDP_ACTIVATED = "sso_idp_activated"
    SSO_IDP_DEACTIVATED = "sso_idp_deactivated"
    SSO_IDP_AUTO_DEACTIVATED = "sso_idp_auto_deactivated"
    SSO_IDP_DELETED = "sso_idp_deleted"
    SSO_IDP_TEST_SUCCEEDED = "sso_idp_test_succeeded"
    SSO_IDP_TEST_FAILED = "sso_idp_test_failed"
    SSO_CROSS_TENANT_IDP_ACCESS_BLOCKED = "sso_cross_tenant_idp_access_blocked"
    SSO_DEFAULT_ROLE_CHANGED = "sso_default_role_changed"
    SSO_DOMAIN_VERIFICATION_STARTED = "sso_domain_verification_started"
    SSO_DOMAIN_VERIFIED = "sso_domain_verified"
    SSO_DOMAIN_VERIFY_SOFT_FAIL = "sso_domain_verify_soft_fail"
    SSO_DOMAIN_RECOVERED = "sso_domain_recovered"
    SSO_DOMAIN_AUTO_DEACTIVATED = "sso_domain_auto_deactivated"
    SSO_DOMAIN_RESOLVER_DISAGREE = "sso_domain_resolver_disagree"
    SSO_DOMAIN_REMOVED = "sso_domain_removed"
    SSO_LOGIN_SUCCESS = "sso_login_success"
    SSO_PASSWORD_BYPASS_USED = "sso_password_bypass_used"
    SSO_LINK_PROMPT_SHOWN = "sso_link_prompt_shown"
    SSO_LINK_ACCEPTED = "sso_link_accepted"
    SSO_LINK_DECLINED = "sso_link_declined"
    SSO_LINK_REVERIFY_SENT = "sso_link_reverify_sent"
    SSO_LINK_REVERIFY_COMPLETED = "sso_link_reverify_completed"
    SSO_LINK_REVERIFY_ABANDONED = "sso_link_reverify_abandoned"
    SSO_ACCOUNT_LINKED = "sso_account_linked"
    SSO_ACCOUNT_MERGED = "sso_account_merged"
    SSO_USER_UNLINKED_SELF = "sso_user_unlinked_self"
    SSO_JIT_USER_PROVISIONED = "sso_jit_user_provisioned"
    SSO_JIT_SEAT_LIMIT_REJECTED = "sso_jit_seat_limit_rejected"
    SSO_JIT_UNKNOWN_DOMAIN_REJECTED = "sso_jit_unknown_domain_rejected"
    SSO_EMAIL_UPDATED_FROM_IDP = "sso_email_updated_from_idp"
    SSO_JIT_RELINKED = "sso_jit_relinked"

    # --- add-scim-directory-sync §2.11 ---
    SCIM_TOKEN_CREATED = "scim_token_created"
    SCIM_TOKEN_ROTATED = "scim_token_rotated"
    SCIM_TOKEN_REVOKED = "scim_token_revoked"
    SCIM_TOKEN_IP_REJECTED = "scim_token_ip_rejected"
    SCIM_TOKEN_LEGACY_USE = "scim_token_legacy_use"

    # GG.HQ.2 — fires when a platform admin sets or clears a tenant's
    # Harbor storage quota override (Tenant.harbor_storage_quota_override_bytes).
    # One row per change; metadata carries the before/after bytes and the
    # source (override vs plan) the next quota sync will use.
    ADMIN_HARBOR_STORAGE_QUOTA_OVERRIDE = "admin_harbor_storage_quota_override"

    # GX.S4 — general email send API (routers/email_send_api.py). Emitted
    # WITHOUT body/attachment content (metadata only: counts + sizes).
    EMAIL_SENT = "email_sent"
    EMAIL_SEND_FAILED = "email_send_failed"

    # GX.S8 — app notification fan-out (routers/notifications.py
    # POST /v1/notifications). Emitted WITHOUT the notification body text
    # (metadata only: counts + channels).
    APP_NOTIFICATION_SENT = "app_notification_sent"

    # GX.A3 — app-key scope + key-expiry enforcement (dependencies.py +
    # services/app_key_scope.py). AUTH_APP_SCOPE_DENIED: an app key hit a
    # route/resource outside its scope (rate-limited per key+route/min).
    # AUTH_API_KEY_EXPIRED: a key with a past expires_at was rejected (the
    # SEC-1-002 fix, applies to ALL keys, not just app keys).
    AUTH_APP_SCOPE_DENIED = "auth_app_scope_denied"
    AUTH_API_KEY_EXPIRED = "auth_api_key_expired"

    # GX.S7 — scheduled HTTP triggers (routers/http_triggers.py,
    # services/http_triggers.py). HTTP_TRIGGER_CREATED: a trigger row was
    # created (metadata only — target identity + schedule kind, never the
    # secret). HTTP_TRIGGER_FIRED: a fire completed (success OR failure,
    # with http status / error in the run-history row). HTTP_TRIGGER_ALERT:
    # the consecutive-failure threshold tripped the three-channel fan-out.
    HTTP_TRIGGER_CREATED = "http_trigger_created"
    HTTP_TRIGGER_FIRED = "http_trigger_fired"
    HTTP_TRIGGER_ALERT = "http_trigger_alert"

    # GX.S10a (launch-completion-megaplan Group X) — a stack variable value was
    # set/rotated via PUT /stacks/{id}/variables/{name}. The audit row carries
    # the variable NAME ONLY (never the value, which lives in Vault).
    STACK_VARIABLE_SET = "stack_variable_set"

    # GX.S1b (launch-completion-megaplan Group X) — app user-type management.
    # APP_USER_TYPE_SET: a type was defined or edited (create/update/soft-delete,
    # folded into one action with a "change" detail). APP_USER_INVITED /
    # APP_USER_DISABLED: the row lifecycle events on app_users.
    APP_USER_TYPE_SET = "app_user_type_set"
    APP_USER_INVITED = "app_user_invited"
    APP_USER_DISABLED = "app_user_disabled"

    # GY.C5 (launch-completion-megaplan) — the SIEM export shipper
    # (services/siem_export.py) could not deliver audit rows: repeated
    # send failure, egress/sealed-mode refusal, or the Vault auth token
    # was unavailable. Written at most once per outage (the shipper
    # resets its flag on the next successful send), so admins get a
    # single SIEM_EXPORT_FAILING row per incident with the reason and
    # endpoint host (never the auth token).
    SIEM_EXPORT_FAILING = "siem_export_failing"

    # GY.C12 (launch-completion-megaplan) — continuous conformance
    # engine drift: a check that previously passed now fails. Written by
    # services.conformance.engine._emit_drift (the single drift hook;
    # notification/webhook/SIEM fan-out is a follow-up).
    CONFORMANCE_DRIFT = "conformance_drift"

    # GY.C15 (launch-completion-megaplan) — legal hold / preserve mode
    # lifecycle (services/legal_hold.py): a hold was placed so retention
    # and purge jobs must skip covered data (preservation requirements,
    # e.g. DFARS 252.204-7012 90-day), and a hold was released (rows are
    # never deleted — release is soft, ISO 27001).
    LEGAL_HOLD_CREATED = "legal_hold_created"
    LEGAL_HOLD_RELEASED = "legal_hold_released"

    # GY.C4 (AC-2/AC-14 emergency access) — the audited break-glass 2FA
    # reset (CORE/startup/break-glass.sh on the manager host). AUTH_
    # BREAK_GLASS fires when an operator removes an admin's TOTP
    # credential(s) and re-queues CONFIGURE_TOTP so the admin re-enrolls
    # on next login (metadata: target email, operator host user, reason,
    # reset_password flag — never secrets). AUTH_BREAK_GLASS_REVIEWED is
    # the mandatory follow-up review (POST /v1/admin/break-glass/{audit
    # id}/review) that the conformance check auth.break_glass_reviewed
    # enforces over a 30-day window.
    AUTH_BREAK_GLASS = "auth_break_glass"
    AUTH_BREAK_GLASS_REVIEWED = "auth_break_glass_reviewed"

    # GY.C4.MFA — the admin "Reset 2FA" action (services/mfa_service.py,
    # POST /v1/admin/users/{id}/mfa/reset): the target's OTP credential(s)
    # are removed in Keycloak and CONFIGURE_TOTP is queued so they re-enroll
    # on the MOD-branded login page. Metadata is metadata-only: actor, target
    # email, reason, credential IDs — never a secret. Append-only (ISO 27001
    # — no hard delete of the audit row or the user row).
    AUTH_MFA_RESET = "auth_mfa_reset"

    # FX.106 (owner Q93=A) — API rate-limit auto-block lifecycle.
    # API_AUTO_BLOCK: a client IP was time-blocked after repeated failed
    # logins or repeated 429 limit hits (services/api_rate_limit_guard.py).
    # API_AUTO_UNBLOCK: a platform admin cleared the block (audited admin
    # action). Metadata carries the ip + reason + unblocks_at — never a
    # secret. Append-only (ISO 27001 — no hard delete of the audit row).
    API_AUTO_BLOCK = "api_auto_block"
    API_AUTO_UNBLOCK = "api_auto_unblock"
    # RATE_LIMIT_EXCEEDED: a per-429 security row when a request is turned
    # away by a plan/key limit. Emitted sparsely (the in-process ring in
    # api_rate_limit_guard.py carries the operational view); the row is the
    # audit-of-record for "who hit the ceiling and when".
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"

    # FX.125 (owner Q112=A) — platform key rotation (part 1 = the
    # inventory + schedules + overdue sweep; the rotation jobs are
    # part 2). KEY_ROTATION_OVERDUE: the daily 04:52 UTC sweep
    # (services/scheduler.py) found a key/cert/token past its keys.*
    # rotation schedule (metadata: item, age_days, due_days, source —
    # never key material). KEY_ROTATED: part 2's rotation jobs record
    # each completed rotation here (item + old/new version identifiers,
    # no key bytes). Append-only audit rows (ISO 27001 — no hard delete).
    KEY_ROTATION_OVERDUE = "key_rotation_overdue"
    KEY_ROTATED = "key_rotated"

    # FX.140 part 3 (owner Q128=A) — a consent decision changed. One row
    # per consent write (signup checkbox, Dashboard preferences PUT,
    # one-click unsubscribe link): metadata = {category, granted, source}
    # — never the email address (PII). Append-only audit rows (ISO 27001).
    EMAIL_CONSENT_CHANGED = "email_consent_changed"

    # FX.93 (owner Q79=A) — abuse reporting backend
    # (services/abuse_reports.py): ABUSE_REPORT_CREATED fires once per
    # report submission; ABUSE_REPORT_TRIAGED fires on every platform
    # triage decision (metadata carries abuse_report_id + the transition:
    # dismissed / actioned / suspend_requested). Never hard-deleted
    # (ISO 27001 — append-only audit rows).
    ABUSE_REPORT_CREATED = "abuse_report_created"
    ABUSE_REPORT_TRIAGED = "abuse_report_triaged"

    # FX.89 (owner Q75=A) — tenant security contact changed. One row per
    # PUT /tenant/security-contact that modified at least one field:
    # metadata carries {changes: {field: {from, to}}, confirmed: bool} —
    # the "to" values are PII the tenant itself set; prior values are
    # recorded for the security incident timeline. Never hard-deleted
    # (ISO 27001 — append-only audit rows).
    TENANT_SECURITY_CONTACT_UPDATED = "tenant_security_contact_updated"

    # FX.98 (owner Q84=A) — likeness consent register
    # (services/likeness_consent.py): LIKENESS_CONSENT_CREATED fires once
    # per consent recorded; LIKENESS_CONSENT_REVOKED on revoke;
    # LIKENESS_CONSENT_DELETED on the register soft-delete (metadata
    # carries likeness_consent_id + transition; never the person name or
    # the scope text). Append-only audit rows (ISO 27001).
    LIKENESS_CONSENT_CREATED = "likeness_consent_created"
    LIKENESS_CONSENT_REVOKED = "likeness_consent_revoked"
    LIKENESS_CONSENT_DELETED = "likeness_consent_deleted"
    # FX.98 — the likeness run-gate fired on a 'likeness'-tagged node:
    # metadata carries transition (run_authorized / run_refused /
    # synthetic_attestation), the reason on refusal, the consent id (the
    # "consent link in the audit trail"), and the node label.
    LIKENESS_RUN_GATED = "likeness_run_gated"

    # FX.97 part 1 (owner decision Q83=A) — a per-output provenance report
    # (JSON) for a workflow run was exported: one row per
    # GET /v1/workflow-runs/{run_id}/provenance. Metadata carries the
    # tenant, run id, workflow id/version and the gap count — never file
    # content or parameter values. Append-only audit rows (ISO 27001).
    PROVENANCE_REPORT_EXPORTED = "provenance_report_exported"

    # FX.67 part 1 (owner decision Q50=A) — customer lockbox (services.
    # lockbox): one row per LockboxRequest state change (REQUESTED /
    # APPROVED / DENIED / REVOKED / EXPIRED) and one row per granted (or
    # denied) cross-tenant access attempt (ACCESS, metadata transition
    # 'granted' / 'denied'). Metadata carries the lockbox_request_id,
    # the staff user id, the target tenant id, the requested reason,
    # scope and expiry — NEVER tenant content itself. Append-only audit
    # rows (ISO 27001).
    LOCKBOX_REQUESTED = "lockbox_requested"
    LOCKBOX_APPROVED = "lockbox_approved"
    LOCKBOX_DENIED = "lockbox_denied"
    LOCKBOX_REVOKED = "lockbox_revoked"
    LOCKBOX_EXPIRED = "lockbox_expired"
    LOCKBOX_ACCESS = "lockbox_access"

    # GSEC FX.153 part 2 (owner decision Q140=B) — in-app personnel
    # security-policy staff register (services/staff_register.py). One
    # audit row per register write: created / updated / soft-deleted /
    # leaver-checklist started / one checklist system closed. Metadata
    # carries the staff_record_id + transition only (names are PII and
    # stay on the register row). Append-only audit rows (ISO 27001).
    STAFF_RECORD_CREATED = "staff_record_created"
    STAFF_RECORD_UPDATED = "staff_record_updated"
    STAFF_RECORD_DELETED = "staff_record_deleted"
    STAFF_LEAVER_CHECKLIST_STARTED = "staff_leaver_checklist_started"
    STAFF_LEAVER_SYSTEM_REMOVED = "staff_leaver_system_removed"

    # GSEC FX.151 part 1 (owner decision Q138=A) — an outbound server-side
    # fetch proceeded to a target matched by the `security.ssrf_allowlist`
    # platform setting (otherwise the SSRF guard in utils/safe_http.py
    # would have refused it). One row per allowlisted fetch; metadata
    # carries the host, tenant_id and reason (host only — never the full
    # URL, which may carry tokens).
    SSRF_ALLOWLIST_FETCH = "ssrf_allowlist_fetch"

    # GSEC FX.156 part 2 — a platform security alert was acknowledged
    # (POST /v1/platform/security-alerts/{id}/ack or
    # POST /v1/tenant/security-alerts/{id}/ack). One row per REAL
    # acknowledgement (re-acks are no-ops — no second row). Metadata
    # carries the alert id, rule key, severity and subject
    # (identifiers only — never credentials/tokens/keys). Append-only
    # audit rows (ISO 27001).
    SECURITY_ALERT_ACKED = "security_alert_acked"

    # GSEC FX.112 p3a — a tenant admin changed the per-tenant config of
    # one built-in FX.156 security alert rule (PUT
    # /v1/tenant/security/alert-rules/{rule_key}): rule enabled/
    # disabled and/or its threshold_override changed within the
    # platform min/max bounds. One row per change; metadata carries
    # rule_key, enabled, threshold_override and the previous values
    # (identifiers + numbers only — never credentials/tokens/keys).
    # Append-only audit rows (ISO 27001).
    TENANT_SECURITY_ALERT_RULE_UPDATED = "tenant_security_alert_rule_updated"

    # GSEC FX.50 part 2 (owner decision Q33=A) — breach-notification
    # incident tracker (services/security_incidents.py). One row per
    # state change: CREATED (row added, status open), CONFIRMED
    # (metadata carries the computed notify_due_at and, for gov/DoD-
    # profile installs, gov_headsup_due_at — clocks only, never breach
    # content), FOLLOWUP_ADDED / FOLLOWUP_COMPLETED (metadata carries
    # the follow-up id + incident id), NOTIFIED (metadata carries
    # notified_at), CLOSED (metadata carries closed_at + note).
    # Identifiers/timestamps only. Append-only audit rows (ISO 27001).
    SECURITY_INCIDENT_CREATED = "security_incident_created"
    SECURITY_INCIDENT_CONFIRMED = "security_incident_confirmed"
    SECURITY_INCIDENT_FOLLOWUP_ADDED = "security_incident_followup_added"
    SECURITY_INCIDENT_FOLLOWUP_COMPLETED = "security_incident_followup_completed"
    SECURITY_INCIDENT_NOTIFIED = "security_incident_notified"
    SECURITY_INCIDENT_CLOSED = "security_incident_closed"

    # GSEC FX.59 part 2 (owner decision Q42=A) — a tenant's contract-end
    # lifecycle state changed (active -> read_only -> suspended, or a
    # reactivation back to active). Written by
    # utils.tenant_lifecycle.set_lifecycle_state. Metadata carries the
    # from/to states and the operator reason.
    TENANT_LIFECYCLE_STATE_CHANGED = "tenant_lifecycle_state_changed"

    # GSEC FX.59 part 3 (owner decision Q42=A) — self-serve full tenant
    # export (services/tenant_export.py). A tenant admin requests the full
    # export zip (workflows, files, outputs, audit log) during the
    # contract-end export window; one row when the job starts and one when
    # a completed package's presigned download is fetched. Metadata carries
    # the job id / object key only — never package content. Append-only
    # audit rows (ISO 27001). Distinct from ADMIN_DATA_EXPORT, which is a
    # PLATFORM-admin export: these rows are written for the tenant by the
    # tenant's own admin, so `action` here is the tenant-facing value.
    TENANT_EXPORT_REQUESTED = "tenant_export_requested"
    TENANT_EXPORT_DOWNLOADED = "tenant_export_downloaded"

    # GSEC FX.49 part 1 (owner decision Q32=A) — break-glass custody
    # (services/break_glass_custody.py). The platform break-glass
    # credential is split into N custodian shares (one half sealed in
    # Vault, one half printed in a sealed envelope in the owner's
    # physical safe); a use request must be approved by a DIFFERENT
    # platform admin before it can be used, and it is single-use. One
    # row per state change: requested / approved / denied / expired
    # (sweep of the approval window) / used. Metadata carries ids,
    # reason and the credential name — never a share or any
    # credential material. Append-only audit rows (ISO 27001).
    BREAK_GLASS_REQUESTED = "break_glass_requested"
    BREAK_GLASS_APPROVED = "break_glass_approved"
    BREAK_GLASS_DENIED = "break_glass_denied"
    BREAK_GLASS_EXPIRED = "break_glass_expired"
    BREAK_GLASS_USED = "break_glass_used"

    # GSEC FX.70 part 2 (owner decision Q53=A) — in-app support ticket
    # lifecycle (services/support_tickets.py). One row per state change:
    # created (auto-ack sent), first response (first_responded_at
    # stamped), resolved (resolved_at stamped). Metadata carries the
    # ticket id, severity, plan tier and due/response timestamps
    # (identifiers only — never credentials/tokens/keys). Append-only
    # audit rows (ISO 27001).
    SUPPORT_TICKET_CREATED = "support_ticket_created"
    SUPPORT_TICKET_FIRST_RESPONSE = "support_ticket_first_response"
    SUPPORT_TICKET_RESOLVED = "support_ticket_resolved"

    # GSEC FX.152 part 2 (owner decision Q139=B) — tenant-admin API-key
    # rotation with a grace window. API_KEY_ROTATED (already defined above,
    # FX.114) records the rotation itself: old key id + new key id.
    # API_KEY_GRACE_EXPIRED is written ONCE, idempotently, when a rotated key
    # is first rejected after its api_keys.rotation_grace_hours window ends
    # (services/api_key_grace.py) — the soft-disable transition. Metadata
    # carries key ids + the window timestamps only, never key material.
    # Append-only audit rows (ISO 27001 — no hard delete of the key row or the
    # audit row).
    API_KEY_GRACE_EXPIRED = "api_key_grace_expired"

    # GSEC FX.95 p1 (owner decision Q81=A) — marketplace listing review
    # state machine (services/listing_review.py). One row per state
    # change: SUBMITTED (submitted -> auto_checks, metadata carries the
    # listing kind + id), APPROVED (staff_review -> live, metadata
    # carries the reviewer id), REJECTED (staff_review -> rejected,
    # metadata carries the rejection reason). Identifiers/reasons only
    # — never listing content. Append-only audit rows (ISO 27001).
    LISTING_SUBMITTED = "listing_submitted"
    LISTING_APPROVED = "listing_approved"
    LISTING_REJECTED = "listing_rejected"

    # GSEC FX.95 p4a (owner decision Q81=A) — publisher identity/company
    # verification behind the Verified badge (services/publisher_verification.py,
    # models.PublisherVerification). One append-only row per transition:
    # REQUESTED (publisher submits identity/company evidence -> 'pending'),
    # APPROVED (platform admin verifies -> 'verified'),
    # REJECTED (platform admin declines -> back to 'unverified'),
    # REVOKED (platform admin withdraws a live verification -> 'revoked';
    # the row is never deleted, so the whole history stays readable).
    # Metadata carries the verification id, tenant id, method and the
    # decision note only — never the evidence documents themselves
    # (ISO 27001 AU-2/AU-3, append-only).
    PUBLISHER_VERIFICATION_REQUESTED = "publisher_verification_requested"
    PUBLISHER_VERIFICATION_APPROVED = "publisher_verification_approved"
    PUBLISHER_VERIFICATION_REJECTED = "publisher_verification_rejected"
    PUBLISHER_VERIFICATION_REVOKED = "publisher_verification_revoked"

    # GSEC FX.73 p2 (owner decision Q56=A) — a platform admin decided a
    # licence_records manual-review queue item (services/licence_review.py).
    # Metadata carries the record id, subject_type/subject_id, the SPDX id
    # and commercial_use the admin set, and an optional note — never
    # licence file content. Append-only audit rows (ISO 27001).
    LICENCE_REVIEW_DECIDED = "licence_review_decided"

    # GSEC FX.56 p1 (owner decision Q39=A) — one append-only row per tenant
    # summarising the audited backfill of expires_at on live credentials
    # that had never carried one (services/credential_lifetime.py
    # backfill_missing_expiries; migration 20261007t). Metadata carries the
    # tenant id, the key count and the lifetime days — never key values.
    CREDENTIAL_EXPIRY_BACKFILL = "credential_expiry_backfill"

    # GSEC FX.31 p2b — the first time a headscale node id is seen in
    # `client.list_nodes()` (services/mesh_acl.py audit_mesh_node_joins,
    # called from apply_acl_policy's node-list sync). One row per join —
    # dedup keyed on the headscale node id via `resource_id`, so a worker's
    # repeat heartbeats/relists never add a second row; a re-join under a
    # new node id (e.g. after a wipe) DOES write a new row. Metadata carries
    # the headscale node id, hostname, tags and user/namespace, and
    # worker_id/stack id when resolvable from the tenant-N mesh user —
    # never key material. Append-only audit rows (ISO 27001).
    MESH_NODE_JOINED = "mesh_node_joined"

    # GSEC FX.76 p5a — the public trust-centre NDA/documentation request
    # form (routers/public_nda_requests.py). One audit row per accepted
    # submission on the platform tenant; metadata carries the request id
    # and the requested doc keys only (never the free text, never an IP).
    NDA_REQUEST_CREATED = "nda_request_created"

    # GSEC FX.79 p1a — data-marking labels (CUI / ITAR / export-controlled /
    # customer-defined) set on a tenant resource (workflow|asset|
    # node_definition|named_artifact|dataset). One audit row per label
    # applied; metadata carries the resource triple + label code only.
    DATA_MARKING_SET = "data_marking_set"

    # GSEC FX.79 p1a — a data marking is cleared (soft-deleted, ISO 27001 —
    # the row is kept). Metadata carries the resource triple + label code.
    DATA_MARKING_CLEARED = "data_marking_cleared"

    # GSEC FX.158 p2a (Q145=A) — a security advisory (CVE-identified) was
    # created / edited / published / soft-deleted by a platform admin.
    # Metadata carries the advisory identifier + severity + publish
    # timestamp only — never the advisory text (ISO 27001 AU-2, append-only).
    SECURITY_ADVISORY_CREATED = "security_advisory_created"
    SECURITY_ADVISORY_UPDATED = "security_advisory_updated"
    SECURITY_ADVISORY_PUBLISHED = "security_advisory_published"
    SECURITY_ADVISORY_DELETED = "security_advisory_deleted"

    # GSEC FX.58 p2b (Q41=A / Q91=A) — one append-only audit-evidence row per
    # automatic restore-test attempt (services/restore_test_job.py). Metadata
    # carries the attempt outcome only: ok, backup id + tested backup
    # timestamp, measured RTO/RPO, duration, the interval window, and the
    # ApiTaskQueue attempt row id — never file content or credentials
    # (ISO 27001 AU-2/AU-3, append-only; no hard deletes).
    BACKUP_RESTORE_TEST_EVIDENCE = "backup_restore_test_evidence"

    # GSEC FX.91 p3b (owner decision Q77=A) — a node's EGRESS ATTEMPT was
    # BLOCKED at the dispatch line: either the gov/DoD allowlist posture
    # (nodes.egress_gov_allowlist_enforced / an active gov compliance
    # profile) refused a declared hostname outside
    # nodes.egress_allowlist_domains, or the tenant-admin one-time approval
    # gate (nodes.egress_approval_required) refused / WOULD refuse while
    # nodes.egress_approval_report_only is on. One append-only row per
    # (workflow run, node, decision) — retries inside
    # security.egress_block_audit_window_seconds are deduped so a run that
    # retries the same blocked node cannot flood the audit trail. Metadata
    # carries tenant, node id/name, the gate + decision, the profile, the
    # denied/declared domain sets, the allowlist and the enforced/report-only
    # posture — hostnames and ids only, never headers, tokens or file
    # content (ISO 27001 AU-2/AU-3, append-only; no hard deletes).
    NODE_EGRESS_BLOCKED = "node_egress_blocked"

    # GSEC FX.120 p2a (owner decision Q107=A) — JIT production admin
    # elevation (services/privileged_elevation.py). There is NO standing
    # admin on production infrastructure (host shell, database admin,
    # Vault admin, Docker): a platform admin files an elevation REQUEST
    # (reason required, one of the four scopes, a TTL defaulting to 2h
    # that auto-revokes) and it is decided by a SECOND person — or, in
    # solo mode, self-approved with an alert + this audit row. One
    # append-only row per state change: requested (created) / approved /
    # denied / activated (the window actually opened) / expired (TTL
    # passed, auto-revoke) / revoked. Metadata carries ids, scope, reason,
    # TTL and the clocks — never a credential, shell command or secret
    # (ISO 27001 AU-2/AU-3, append-only; no hard deletes).
    PRIVILEGED_ELEVATION_REQUESTED = "privileged_elevation_requested"
    PRIVILEGED_ELEVATION_APPROVED = "privileged_elevation_approved"
    PRIVILEGED_ELEVATION_DENIED = "privileged_elevation_denied"
    PRIVILEGED_ELEVATION_ACTIVATED = "privileged_elevation_activated"
    PRIVILEGED_ELEVATION_EXPIRED = "privileged_elevation_expired"
    PRIVILEGED_ELEVATION_REVOKED = "privileged_elevation_revoked"

    # GSEC FX.119 p2a (owner decision Q106=A) — the on-call PAGING CHANNEL
    # (services/paging_service.py). One append-only row per page OUTCOME:
    # ONCALL_PAGE_SENT (the self-hosted ntfy push stack accepted the POST) and
    # ONCALL_PAGE_FAILED (disabled, no channel configured, no Vault publisher
    # token, timeout, or ntfy 4xx/5xx). Metadata carries the page title
    # excerpt, topic, ntfy priority, the monitor's reason and the HTTP status
    # — identifiers and outcomes only, never the publisher token and never a
    # full page body (ISO 27001 AU-2/AU-3, append-only; no hard deletes).
    ONCALL_PAGE_SENT = "oncall_page_sent"
    ONCALL_PAGE_FAILED = "oncall_page_failed"

    # GSEC FX.119 p3a/p3b (owner decision Q106=A) — the ACK + ESCALATION half
    # of the on-call rota. ONCALL_PAGE_ACKED (the on-call person tapped the
    # ack link on their phone: models.OncallPage flips sent -> acked, the
    # single-use token is consumed and can never be replayed) and
    # ONCALL_PAGE_ESCALATED (nobody acked inside oncall.ack_window_seconds —
    # the scheduler tick re-pages oncall.owner_topic exactly once and marks
    # the page escalated). ONCALL_NO_ROTA names the window where a page had
    # to fall back to the default topic because no rota is configured — one
    # row per window, never a flood (ISO 27001 AU-2/AU-3, append-only).
    ONCALL_PAGE_ACKED = "oncall_page_acked"
    ONCALL_PAGE_ESCALATED = "oncall_page_escalated"
    ONCALL_NO_ROTA = "oncall_no_rota"

    # GSEC FX.108 p1 (owner decision Q95=A) — hosted sign-up AGE ATTESTATION.
    # AGE_ATTESTATION_RECORDED fires once per hosted sign-up that carries the
    # 16+ tick (the record of "this account was created with an age attestation
    # at this time"); SIGNUP_UNDERAGE_BLOCKED names a hosted sign-up refused
    # because the attested age is under the minimum (metadata carries the
    # BAND — 'under_13' / 'minor' — never a birth year, never a name/email,
    # so the refusal is auditable without storing personal data for an
    # account that was never created; ISO 27001 AU-2/AU-3, append-only).
    AGE_ATTESTATION_RECORDED = "age_attestation_recorded"
    SIGNUP_UNDERAGE_BLOCKED = "signup_underage_blocked"

    # BRIEF-EXTFARM-1 — farm registry (services/external_farms.py). Emitted by
    # brief 2's router on register/delete. Metadata carries kind + name only;
    # NEVER the endpoint credential (the secret lives in Vault KV v2 and only
    # its reference path is stored on the row; ISO 27001 AU-2, append-only).
    EXTERNAL_FARM_CREATED = "external_farm_created"
    EXTERNAL_FARM_DELETED = "external_farm_deleted"


class PermissionAuditLog(Base):
    """
    Audit log for all permission-related and security operations.

    Tracks who did what, when, and provides context for compliance and debugging.

    CMMC/ISO 27001 Compliance: AU-9 (Protection of Audit Information)
    - Includes HMAC signature for tamper detection
    - Previous record hash for chain integrity verification
    """
    __tablename__ = "permission_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)

    # Action details
    action = Column(Enum(AuditAction), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Who performed the action
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Who was affected (optional - may be null for permission requests)
    target_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # What resource was involved
    resource_type = Column(String, nullable=False, index=True)  # "cog", "workflow", "security", etc.
    resource_id = Column(Integer, nullable=False, index=True)

    # Permission details
    permission_level = Column(String, nullable=True)  # "viewer", "editor", "owner"

    # Related records
    # Gate 30: legacy `permissions` table retired; this holds the read-model grant
    # id (a ResourcePermission.id) going forward, so it is a plain int, not an FK.
    permission_id = Column(Integer, nullable=True)
    permission_request_id = Column(Integer, ForeignKey("permission_requests.id"), nullable=True)

    # Additional context (stores before/after state, justification, notes, etc.)
    # Note: Named 'audit_metadata' to avoid conflict with SQLAlchemy's reserved 'metadata' attribute
    audit_metadata = Column(JSONB, nullable=True)

    # IP and user agent for security tracking
    ip_address = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)

    # CMMC AU-9: Integrity verification fields
    # HMAC signature of record content for tamper detection
    integrity_hash = Column(String(64), nullable=True)
    # Hash of previous record for chain verification (blockchain-style)
    previous_hash = Column(String(64), nullable=True)

    # Relationships
    tenant = relationship("Tenant")
    actor = relationship("User", foreign_keys=[actor_user_id])
    target_user = relationship("User", foreign_keys=[target_user_id])
    # Gate 30: permission_id now holds a read-model grant id (a
    # ResourcePermission.id), not a legacy permissions.id — no ORM relationship.
    permission_request = relationship("PermissionRequest")


# =============================================================================
# AI Conversation Models
# =============================================================================

class AIConversation(Base):
    """
    AI conversation state for multi-turn log analysis.
    Tracks conversation history and worker assignment for sticky routing.
    """
    __tablename__ = "ai_conversations"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4, index=True)

    # Context
    workflow_run_id = Column(Integer, nullable=True)  # Optional - conversation might not be about specific run
    node_kind = Column(String, nullable=True)  # node-type identifier (NodeDefinition.node_kind)
    orchestrator_run_id = Column(String, nullable=True)  # workflow run identifier
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)

    # Worker assignment for sticky routing
    assigned_worker_id = Column(String, nullable=True)  # Which AI worker has this conversation loaded

    # Conversation history (array of message objects)
    # Format: [{"role": "user", "content": "...", "timestamp": "..."}, {"role": "assistant", "content": "...", "timestamp": "..."}]
    messages = Column(JSONB, nullable=False, default=list)

    # Metadata
    title = Column(String, nullable=True)  # Auto-generated from first message
    last_activity = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())
    created_at = Column(DateTime, nullable=False, default=func.now())

    # Soft delete
    deleted_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User")
    tenant = relationship("Tenant")


# =============================================================================
# Worker License Management
# =============================================================================

class WorkerOwnerType(str, enum.Enum):
    """Worker owner type for 4-tier worker model"""
    PLATFORM_ONLY = "platform_only"      # Platform-only workers (no tenant access)
    PLATFORM_POOL = "platform_pool"      # Shared pool available to all tenants
    PLATFORM_ASSIGNED = "platform_assigned"  # Assigned to specific tenants
    TENANT_OWNED = "tenant_owned"        # BYOH workers for specific tenant


class WorkerStatus(str, enum.Enum):
    """Worker license status"""
    UNLICENSED = "unlicensed"  # Worker connected but no license assigned
    LICENSED = "licensed"       # Worker has active license
    OFFLINE = "offline"         # Worker was licensed but now disconnected


class Manager(SoftDeleteMixin, Base):
    """Peer-manager registry for the multi-region mesh (add-multi-region-mesh).

    One row per manager in the mesh. load_snapshot holds the latest gossiped
    load/liveness. Empty on a single-manager deployment, so all existing
    behavior is unchanged until peers are enrolled."""
    __tablename__ = "managers"

    manager_id = Column(String(255), primary_key=True, index=True)
    region = Column(String(100), nullable=True, index=True)
    zone = Column(String(100), nullable=True)
    url = Column(String(512), nullable=False)         # PEER/internal address (gossip/handoff)
    external_url = Column(String(512), nullable=True)  # PUBLIC, LB-fronted address (clients/display)
    # `status` is GOSSIP-owned (active/stale) — set automatically by the heartbeat
    # sweep. `admin_state` is ADMIN-owned (pending/approved/revoked) — set only by
    # a human via the approve/revoke endpoints. They are deliberately separate so a
    # revoke is never overwritten by the next heartbeat. Only approved managers
    # participate in gossip/handoff/promotion.
    status = Column(String(20), default="active", nullable=False, index=True)
    admin_state = Column(String(20), default="pending", nullable=False, index=True)
    enrolled_by_user_id = Column(Integer, nullable=True)
    approved_by_user_id = Column(Integer, nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    last_seen = Column(DateTime(timezone=True), nullable=True)
    load_snapshot = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MeshSelfConfig(Base):
    """Admin-set overrides for THIS manager's own advertised addresses (singleton).
    `MANAGER_SELF_URL`/`MANAGER_EXTERNAL_URL` env are the boot defaults; once an admin
    sets these in the UI they win (and survive restarts, unlike env), so the peer URL
    can be corrected to the VM's routable IP without editing env + recreating."""
    __tablename__ = "mesh_self_config"

    singleton_id = Column(Integer, primary_key=True, default=1)
    peer_url = Column(String(512), nullable=True)       # routable inter-manager address
    external_url = Column(String(512), nullable=True)   # public/LB-fronted address
    updated_by_user_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ManagerEnrollmentToken(Base):
    """One-time enrollment token for adding a manager to the mesh — the control-
    plane analog of WorkerEnrollmentToken. An admin generates one (with an assigned
    manager_id + region) and gets a paste-able bootstrap command; the new VM redeems
    it to receive the shared mesh trust token + its identity, then enrolls + waits
    for approval. Stored hashed; single-use; expiring."""
    __tablename__ = "manager_enrollment_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String(255), unique=True, nullable=False, index=True)
    manager_id = Column(String(255), nullable=False)   # the id the new manager will take
    region = Column(String(100), nullable=True)
    zone = Column(String(100), nullable=True)
    external_url = Column(String(512), nullable=True)  # the new manager's public/LB URL
    label = Column(String(255), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    redeemed_at = Column(DateTime(timezone=True), nullable=True)   # one-time
    redeemed_self_url = Column(String(512), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MeshReplicationHeartbeat(Base):
    """One row per manager (manager_id = self), bumped each gossip round. Cross-
    region replication copies it to peers; a peer reads this manager's row and
    computes now() - updated_at = the actual end-to-end replication lag (the RPO
    data-loss window). A missing/stale peer row = replication down (RPO breach)."""
    __tablename__ = "mesh_replication_heartbeat"

    manager_id = Column(String(255), primary_key=True)
    region = Column(String(100), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MeshOwnershipEvent(Base):
    """Durable provenance for every work-ownership transition in the mesh
    (add-multi-region-mesh). The Redis fence holds only the CURRENT owner/token;
    this table is the append-only HISTORY: who moved what work, from which manager
    to which, with what fencing token, by which actor, and why. Critical for
    reconstructing a multi-region incident (handoffs, failover/promotion)."""
    __tablename__ = "mesh_ownership_events"

    id = Column(Integer, primary_key=True, index=True)
    work_key = Column(String(512), nullable=False, index=True)
    from_manager = Column(String(255), nullable=True)
    to_manager = Column(String(255), nullable=False)
    fencing_token = Column(Integer, nullable=True)
    action = Column(String(32), nullable=False)   # handoff | promote | auto_promote | claim | accept_handoff
    actor_user_id = Column(Integer, nullable=True)
    reason = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class WorkLoan(SoftDeleteMixin, Base):
    """manager-work-loans — durable record of a run work-loan between managers.

    Replaces the shared-Redis fence in services/mesh_handoff_service.py: the
    initiating manager stays the PERMANENT owner of a workflow run; a peer
    (borrower) manager executes under a time-bounded lease. The loan row is the
    source of truth — `owner_manager_id` is immutable for the life of the row
    (ORM `@validates` guard + a BEFORE UPDATE trigger in the migration), and
    `lease_expires_at` bounds how long the borrower may hold the work."""
    __tablename__ = "work_loans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('granted', 'running', 'claimed', 'collected', 'expired', "
            "'cancelled', 'rolled_back')",
            name="ck_work_loans_status",
        ),
        Index("ix_work_loans_workflow_run_id", "workflow_run_id"),
        Index("ix_work_loans_status_lease_expires_at", "status", "lease_expires_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    workflow_run_id = Column(ForeignKey("workflow_runs.id"), nullable=False)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    owner_manager_id = Column(ForeignKey("managers.manager_id"), nullable=False)
    borrower_manager_id = Column(ForeignKey("managers.manager_id"), nullable=False)
    lease_expires_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default="granted", server_default="granted", index=True)
    # Reference to the assertion/claim record that justified the grant
    # (e.g. the manager ACL assertion or handoff token ref).
    assertion_ref = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), index=True)  # soft delete (ISO 27001: no hard deletes)

    @validates("owner_manager_id")
    def _owner_manager_id_immutable(self, key, value):
        # owner_manager_id is set once at grant and may never change on a
        # persisted row. @validates fires only for explicit attribute sets, so
        # the initial constructor set passes through while the instance is
        # still pending; any later set after the row is flushed raises. (The
        # migration's BEFORE UPDATE trigger enforces the same rule for raw
        # SQL / other sessions the ORM guard can't see.)
        if _sa_inspect(self).persistent:
            raise ValueError(
                "work_loans.owner_manager_id is immutable after insert "
                "(manager-work-loans: the initiating manager stays the owner)"
            )
        return value


class MeshLoanEvent(Base):
    """Append-only provenance for work-loan state transitions
    (manager-work-loans). Mirrors MeshOwnershipEvent: every grant / status
    change on a work_loan row is recorded here — who changed what, from which
    manager to which, with which assertion ref, by which actor, and why.
    Critical for reconstructing a multi-region incident (a run executed on a
    peer under a lease; the lease expired; the owner reclaimed)."""
    __tablename__ = "mesh_loan_events"

    id = Column(Integer, primary_key=True, index=True)
    work_loan_id = Column(ForeignKey("work_loans.id"), nullable=False, index=True)
    workflow_run_id = Column(ForeignKey("workflow_runs.id"), nullable=True, index=True)
    # GC.MWL.14: widened 20 -> 64. VARCHAR(20) was too short for some
    # transition names (e.g. 'checkpoint_export_requested', 27 chars) —
    # see migrations/20260930_widen_mesh_loan_event_status.py.
    from_status = Column(String(64), nullable=True)
    to_status = Column(String(64), nullable=False)
    from_manager = Column(String(255), nullable=True)
    to_manager = Column(String(255), nullable=True)
    assertion_ref = Column(String(512), nullable=True)
    actor_user_id = Column(Integer, nullable=True)
    reason = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class ManagerExecuteGrant(SoftDeleteMixin, Base):
    """manager-work-loans (GC.MWL.2) — separates "may open a SESSION on this
    manager" from "may EXECUTE work here". WorkLoan lets a borrowing manager
    run a loaned workflow run AS the submitting user; this table is the
    authorization record that decides whether that user is even allowed to
    execute (or merely log in) on that specific manager, in that specific
    tenant.

    `source` + nullable `idp_group` model BOTH possible grant origins the
    product owner hasn't decided between yet, so either answer fits without
    a schema change:
      * source='manual', idp_group=NULL    — an admin grants a MOD role
        directly (`granted_by` records who).
      * source='idp_group', idp_group=<n>  — membership in IdP group <n>,
        synced via SCIM, implies the grant.

    Revocation is a soft delete (ISO 27001: no hard deletes) via
    SoftDeleteMixin's `deleted_at`; a grant with `deleted_at` set is inert.
    Defaults REFUSE: services/mesh_execute_grants.py only authorizes when a
    live (not soft-deleted), tenant-matching row with the right scope
    exists — no grant, wrong tenant, or wrong scope all refuse."""
    __tablename__ = "manager_execute_grants"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('execute', 'login', 'both')",
            name="ck_manager_execute_grants_scope",
        ),
        CheckConstraint(
            "source IN ('manual', 'idp_group')",
            name="ck_manager_execute_grants_source",
        ),
        Index(
            "ux_manager_execute_grants_live",
            "manager_id", "user_id", "tenant_id", "scope",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    manager_id = Column(ForeignKey("managers.manager_id"), nullable=False, index=True)
    user_id = Column(ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    scope = Column(String(20), nullable=False)
    source = Column(String(20), nullable=False, default="manual", server_default="manual")
    idp_group = Column(String(255), nullable=True)
    granted_by = Column(ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), index=True)  # soft delete (ISO 27001: no hard deletes)


class MeshRevocation(Base):
    """manager-work-loans (GC.MWL.3) — revocation gossip: a CRDT-style
    last-writer-wins register keyed by (tenant_id, user_id). `revoked` is
    the CURRENT effective decision, resolved by the HIGHEST
    (version_epoch_us, version_origin_manager_id) tuple seen from ANY
    manager — local SCIM event or gossiped from a peer — regardless of
    arrival order: a reinstate always beats an OLDER revoke, but an OLDER
    reinstate can never undo a NEWER revoke, and re-applying the same (or a
    lower) version is a no-op. See services/mesh_revocation_service.py.

    A dedicated table rather than a JSONB blob on Manager: `can_execute`
    (services/mesh_execute_grants.py) reads this per (tenant_id, user_id) on
    every loaned-execute authorization check, a hot path — an indexed row
    lookup beats parsing/scanning a manager-wide JSONB blob on every call,
    and the partition (single unique row per key) is exactly the CRDT
    register's own state, not an append log.

    `tenant_id`/`user_id` are deliberately NOT foreign keys: a gossiped
    entry about a user on a peer manager may name a (tenant, user) this
    manager has never locally provisioned (a partitioned/minority manager
    can hear about a revocation before, or without ever, seeing the user
    row itself) — the whole point of gossip is to keep hearing regardless
    of what local state exists."""
    __tablename__ = "mesh_revocations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_mesh_revocations_tenant_user"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    revoked = Column(Boolean, nullable=False)
    version_epoch_us = Column(BigInteger, nullable=False)
    version_origin_manager_id = Column(String(255), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)


class WorkLoanCheckpointTransfer(SoftDeleteMixin, Base):
    """manager-work-loans (GC.MWL.C1) — durable record of one checkpoint
    transfer between managers under a work loan.

    A loaned node run may hand its checkpoint to the borrower (direction
    'export' on the owner side) and the borrower hands it back ('import').
    Each attempt is its own row: `attempt` is the 1-based retry count for the
    (node_run_id, direction) pair, `object_key` names the uploaded object once
    the transfer has a payload, `byte_size` / `progress_at_transfer` track what
    was actually moved, and `status` walks pending -> uploaded -> imported
    (or failed). Like every work-loans table, rows are soft-deleted (ISO
    27001: no hard deletes) — a deleted transfer record stays auditable."""
    __tablename__ = "work_loan_checkpoint_transfers"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('export', 'import')",
            name="ck_work_loan_checkpoint_transfers_direction",
        ),
        CheckConstraint(
            "status IN ('pending', 'uploaded', 'imported', 'failed')",
            name="ck_work_loan_checkpoint_transfers_status",
        ),
        Index("ix_work_loan_checkpoint_transfers_work_loan_id", "work_loan_id"),
        Index(
            "ix_work_loan_checkpoint_transfers_node_run_id_attempt",
            "node_run_id", "attempt",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    work_loan_id = Column(ForeignKey("work_loans.id"), nullable=False)
    node_run_id = Column(ForeignKey("node_runs.id"), nullable=False)
    attempt = Column(Integer, nullable=False, default=1, server_default="1")
    direction = Column(String(20), nullable=False)
    object_key = Column(String(512), nullable=True)
    byte_size = Column(BigInteger, nullable=True)
    progress_at_transfer = Column(Float, nullable=True)
    status = Column(String(20), nullable=False, default="pending", server_default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), index=True)  # soft delete (ISO 27001: no hard deletes)


class WorkerLicense(Base):
    """
    Tracks mod-worker (Go) instances and their license assignments.

    Workers auto-register on first heartbeat as "unlicensed". Platform
    admin assigns licenses via UI to activate them.
    """
    __tablename__ = "worker_licenses"

    id = Column(Integer, primary_key=True, index=True)

    # Worker identification
    worker_name = Column(String(255), unique=True, nullable=False, index=True)  # Worker identifier (e.g., "gpu-server-1")
    hostname = Column(String(255), nullable=False, index=True)  # Hostname of the machine

    # Hardware specifications (detected at discovery)
    hardware_specs = Column(JSONB, nullable=True)  # {ram_gb: 64, gpu_type: "CUDA", vram_gb: 24, cpu_cores: 32}
    queues = Column(JSONB, nullable=True)  # ["CUDA_Large", "CUDA_Mid", "CPU_Large"]

    # License status
    status = Column(Enum(WorkerStatus), default=WorkerStatus.UNLICENSED, nullable=False, index=True)

    # License assignment tracking
    licensed_at = Column(DateTime(timezone=True), nullable=True)
    licensed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Worker ownership tracking (4-tier model)
    owner_type = Column(Enum(WorkerOwnerType), default=WorkerOwnerType.PLATFORM_POOL, nullable=False, index=True)
    owner_tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    assigned_tenant_ids = Column(SafeARRAY, nullable=True)  # Multiple tenant assignments

    # Worker activity tracking
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_heartbeat = Column(DateTime(timezone=True), nullable=True)  # Updated by background task

    # Remote worker support
    is_remote = Column(Boolean, default=False, nullable=False)  # True = remote BYOH worker, False = on-prem pool worker
    agent_version = Column(String(50), nullable=True)  # Installed worker agent version (e.g., "2.4.1")
    max_concurrent_tasks = Column(Integer, default=0, nullable=False)  # 0 = unlimited, >0 = cap on CLAIMED+RUNNING tasks

    # Gate 7 — what running this machine costs its operator, per hour.
    #
    # Set per worker, in the UI, by whoever owns the hardware. The same card in
    # two buildings has different electricity behind it, and an on-prem
    # customer's costs are theirs rather than ours, so there is no sensible
    # platform-wide default and NULL means "not priced yet" rather than free.
    #
    # cost_per_hour_cents is the single source of truth for pricing. The four
    # fields below it are the workings, kept so the figure can be audited and
    # re-derived when electricity moves — a bare rate with no breakdown is a
    # number nobody can defend a year later.
    cost_per_hour_cents = Column(Integer, nullable=True)
    hardware_cost_cents = Column(BigInteger, nullable=True)
    amortisation_months = Column(Integer, nullable=True)
    power_watts = Column(Integer, nullable=True)
    electricity_cents_per_kwh = Column(Integer, nullable=True)
    # What to charge ON TOP of cost, as a percentage. 20 means bill 1.2x COGS.
    # NULL means "use the platform default" rather than "sell at cost" — an
    # unset margin must not silently mean zero profit.
    margin_percent = Column(Integer, nullable=True)

    # Drain / pause control. When True, heartbeat response sets drain=true; the
    # worker stops claiming new tasks and, once active tasks finish, triggers
    # self-update if a newer binary is available. Operator flips this via the
    # Dashboard Pause/Resume buttons — it's per-worker, independent of the
    # global server_updating flag.
    paused = Column(Boolean, default=False, nullable=False, index=True)

    @staticmethod
    def _on_paused_set(target, value, oldvalue, initiator):
        if value and not oldvalue:
            import traceback, logging
            _lg = logging.getLogger("models.WorkerLicense")
            stack = "".join(traceback.format_stack()[-8:-1])
            _lg.warning(
                "PAUSE TRAP: %s paused flipped False→True via ORM\n%s",
                getattr(target, "worker_name", "?"), stack,
            )

    # Update request — a ONE-SHOT drain + self-update + auto-resume cycle
    # triggered by the Dashboard Update button. Separate from `paused` because
    # the operator intent is different: Update means "finish current work,
    # swap binary, come back online" and should auto-clear once the new
    # version heartbeats. Pause is sticky; the operator must manually Resume.
    # Heartbeat OR's both into `drain`, so the Go worker's existing drain
    # logic handles either signal identically.
    update_requested = Column(Boolean, default=False, nullable=False, index=True)

    # Network zone — used for routing tasks to workers that can reach specific endpoints
    # e.g. "office", "dc-east", "cloud". "public" is reserved (reachable from anywhere).
    network_zone = Column(String(50), nullable=True, default="local")

    # GY.V2b (export control, GSEC R-040) — the worker is US-located and
    # US-person-operated. NULL or False = NOT certified, which for claim
    # purposes is the same as False: tasks whose inputs carry EAR/ITAR
    # (and CUI when compliance.cui_requires_us_person is on) are claimable
    # ONLY by workers with this flag TRUE (_try_claim_task's
    # export_control_guard); with no such worker the task simply waits.
    # Set by platform admin via PATCH /workers/{worker_name}/export-control.
    us_person_operated = Column(Boolean, nullable=True, index=True)

    # NOAH opportunistic idle-harvest per-worker overrides. NULL = inherit the
    # PlatformNoahSettings default (resolution: this row -> platform default).
    # A worker with noah_enabled resolved false is a normal (dedicated) worker.
    noah_enabled = Column(Boolean, nullable=True)
    noah_sustained_quiet_s = Column(Integer, nullable=True)
    noah_pause_grace_s = Column(Integer, nullable=True)
    noah_plugged_in_only = Column(Boolean, nullable=True)
    noah_schedule_start = Column(String(5), nullable=True)  # "HH:MM" local
    noah_schedule_end = Column(String(5), nullable=True)
    noah_batch_appetite = Column(Integer, nullable=True)  # max pre-placed tasks; 0 = pull-only
    # Last reported opportunistic availability (from the worker heartbeat).
    noah_available = Column(Boolean, nullable=True)
    noah_reason = Column(String(64), nullable=True)
    # noah-observability: when the availability last flipped (idle<->busy) and
    # the cumulative count of never-started tasks harvested (released) off this
    # worker. Both feed the fleet NOAH observability endpoint so transitions are
    # visible instead of buried in a per-worker modal.
    noah_available_since = Column(DateTime(timezone=True), nullable=True)
    # Worker's self-reported "next free at" hint (WS3.26): earliest time it
    # expects to be free again; NULL = no hint.
    noah_next_free_at = Column(DateTime(timezone=True), nullable=True)
    noah_harvested_total = Column(Integer, nullable=False, default=0, server_default="0")

    # Notes and metadata
    notes = Column(Text, nullable=True)  # Admin notes about this worker

    # FX.126 (Q113=A) — physical-asset register metadata (admin-set, audited
    # via PATCH /v1/admin/assets/workers/{name}). NULL = not classified yet.
    owner_name = Column(String(255), nullable=True)      # named asset owner
    data_class = Column(String(16), nullable=True)       # public|internal|confidential|cui
    site_region = Column(String(100), nullable=True)     # e.g. "dc-east", "nyc-office"

    # FX.139 part 3 (docs/isms/14 — media sanitisation/disposal policy) — the
    # disposal record linked to the asset-inventory row. NULL = never sanitised.
    disposal_serial = Column(String(255), nullable=True)  # serial at disposal
    disposal_date = Column(DateTime(timezone=True), nullable=True)  # when sanitised
    disposal_method = Column(String(40), nullable=True)   # crypto_erase|physical_destroy|crypto_erase_then_destroy|returned_keep_drive
    disposal_certificate_ref = Column(Text, nullable=True)  # certificate id or URL (not a file)

    # Hardware profile (dedup — identical hardware shares one profile)
    hardware_profile_id = Column(Integer, ForeignKey("hardware_profiles.id"), nullable=True)

    # BUG-1029-worker-hard-delete (ISO 27001 no-hard-delete): DELETE /workers/{name}
    # tombstones instead of reaping. `deleted_at` is the tombstone stamp,
    # `deleted_by_user_id` names who decommissioned it (audit chain). Every listing
    # /lookup filters `deleted_at IS NULL`; the WorkerAssignmentHistoryRecord +
    # WorkerResourceTimeline rows are never deleted.
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    deleted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # NOTE: `worker_name` stays PLAIN unique — `persistent_stacks.target_worker` and
    # `worker_resource_timelines.worker_name` are FKs to it, and Postgres requires a
    # plain UNIQUE constraint as the FK target (a partial unique index cannot satisfy
    # one). Re-enrolling a decommissioned worker under the SAME name therefore needs
    # the tombstone reactivated in the enrollment path (services scope, tracked as the
    # iso-no-hard-delete follow-up); the row itself is never reaped.
    # Relationships
    licensed_by = relationship("User", foreign_keys=[licensed_by_user_id])
    revoked_by = relationship("User", foreign_keys=[revoked_by_user_id])
    owner_tenant = relationship("Tenant", foreign_keys=[owner_tenant_id])
    hardware_profile = relationship("HardwareProfile", foreign_keys=[hardware_profile_id])
    deleted_by = relationship("User", foreign_keys=[deleted_by_user_id])


from sqlalchemy import event as _sa_event
_sa_event.listen(WorkerLicense.paused, "set", WorkerLicense._on_paused_set)
# node-role §1 — system_only is kept in sync with node_role via the
# `@validates` on NodeDefinition.node_role (see the class), not a table event.


class WorkerUpgradeCampaign(Base):
    """FX.62 p3a2 — the API-side ROLLING worker upgrade (services/worker_update_rolling.py).

    One campaign per release rollout. The campaign is the memory the rollout
    needs and `worker_licenses.update_requested` never had: which version we
    are rolling to, which workers are in it, and — when one of them fails —
    WHY the rollout stopped. ISO 27001: no hard delete; `deleted_at` is the
    soft delete and a `held` campaign is a STATE change, never a wipe.

    status: open (still rolling) | held (stopped on a failure, reason in
    hold_reason) | completed (every step healthy/skipped) | abandoned.
    """
    __tablename__ = "worker_upgrade_campaigns"

    id = Column(Integer, primary_key=True, index=True)

    target_version = Column(String(50), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="open",
                    server_default="open", index=True)  # open|held|completed|abandoned
    hold_reason = Column(Text, nullable=True)
    held_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class WorkerUpgradeStep(Base):
    """FX.62 p3a2 — one row per worker in a rolling upgrade campaign.

    The step IS the per-worker sequencing record: `queued` workers wait,
    exactly `worker_update.batch_size` of them are `requested` at a time
    (update_requested set on the worker row), a requested worker becomes
    `healthy` when its heartbeat stamps the campaign's target version
    (FX.62 p3a1's version stamp), and a worker that errors or never comes
    back within `worker_update.health_timeout_seconds` becomes `failed`/
    `timeout` and HOLDS the campaign. `skipped` is operator intent (paused
    worker), which is not a failure and must not hold the rollout.

    FKs carry NO `ON DELETE` clause: the record of who was upgraded to
    what stays even if the worker row is later soft-deleted (A.18.1.3).
    """
    __tablename__ = "worker_upgrade_steps"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("worker_upgrade_campaigns.id"),
                         nullable=False, index=True)
    worker_license_id = Column(Integer, ForeignKey("worker_licenses.id"),
                               nullable=False, index=True)
    worker_name = Column(String(255), nullable=False)

    from_version = Column(String(50), nullable=True)
    to_version = Column(String(50), nullable=False)
    state = Column(String(16), nullable=False, default="queued",
                   server_default="queued", index=True)  # queued|requested|healthy|failed|timeout|skipped
    requested_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    attempt = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class HardwareProfile(Base):
    """Hardware profile for deduplicating identical hardware across workers.

    Workers with the same CPU+GPU+RAM fingerprint share one profile.
    Benchmark scores are computed once per profile, not per worker.
    """
    __tablename__ = "hardware_profiles"

    id = Column(Integer, primary_key=True, index=True)
    fingerprint = Column(String(64), unique=True, nullable=False, index=True)

    cpu_model = Column(String(255), nullable=True)
    cpu_cores = Column(Integer, nullable=True)
    gpu_model = Column(String(255), nullable=True)
    gpu_vram_gb = Column(Float, nullable=True)
    ram_gb = Column(Float, nullable=True)

    cloud_provider = Column(String(20), nullable=True)  # aws, gcp, azure, null=on-prem
    instance_type = Column(String(100), nullable=True)  # e.g. "g5.xlarge"

    gpu_compute_score = Column(Float, nullable=True)
    cpu_multi_score = Column(Float, nullable=True)
    cpu_single_score = Column(Float, nullable=True)
    io_seq_score = Column(Float, nullable=True)
    io_random_score = Column(Float, nullable=True)
    memory_bw_score = Column(Float, nullable=True)
    minio_download_mbps = Column(Float, nullable=True)
    minio_upload_mbps = Column(Float, nullable=True)
    docker_pull_seconds = Column(Float, nullable=True)
    container_startup_ms = Column(Float, nullable=True)
    gpu_memory_bw_score = Column(Float, nullable=True)
    disk_seq_read_score = Column(Float, nullable=True)

    benchmark_status = Column(String(20), default="pending", nullable=False)  # pending, running, completed, skipped, failed
    benchmarked_at = Column(DateTime(timezone=True), nullable=True)
    benchmark_results = Column(JSONB, nullable=True)

    # AI scheduler — per-profile calibration freshness (separate from per-NodeDefinition
    # calibration). When 'stale' the staleness eval job has detected drift on this
    # profile (driver/kernel/package change OR threshold age) and the orchestrator
    # auto-queues recalibration. Distinct from benchmark_status (one-time hardware
    # benchmark scoring vs. ongoing per-task-type runtime calibration).
    calibration_status = Column(String(20), default="uncalibrated", nullable=False, server_default="uncalibrated")
    last_calibrated_at = Column(DateTime(timezone=True), nullable=True)

    # One-shot HMAC key for benchmark-result signature verification.
    # Primary storage is Vault (api-benchmark-hmac policy), but when Vault
    # isn't writable — e.g. the existing deployment is running without the
    # policy that vault-init.sh now installs — we fall back to this column.
    # Cleared the moment verification succeeds so the key never lives
    # longer than one benchmark run.
    benchmark_hmac_key = Column(String(128), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AcceleratorProfile(Base):
    """One row per physical accelerator device per worker hardware fingerprint.

    When a worker heartbeats with an `accelerators` list, the API upserts rows
    here keyed by (hardware_profile_id, kind, index). Benchmark scores are
    populated when the per-device benchmark task completes.
    """
    __tablename__ = "accelerator_profiles"

    id = Column(Integer, primary_key=True, index=True)
    hardware_profile_id = Column(Integer, ForeignKey("hardware_profiles.id"), nullable=False, index=True)

    kind = Column(String(16), nullable=False, index=True)
    index = Column(Integer, nullable=False)
    name = Column(String(255), nullable=True)
    vendor_id = Column(String(8), nullable=True)
    device_id = Column(String(8), nullable=True)
    uuid = Column(String(64), nullable=True)
    pci_bus_id = Column(String(32), nullable=True)
    driver_version = Column(String(64), nullable=True)
    # Driver version snapshotted at last calibration. Compared on every heartbeat;
    # mismatch flips the parent HardwareProfile to calibration_status='stale'.
    baseline_driver_version = Column(String(64), nullable=True)
    vram_gb = Column(Float, nullable=True)

    compute_score = Column(Float, nullable=True)
    memory_bw_score = Column(Float, nullable=True)
    copy_bw_score = Column(Float, nullable=True)
    benchmark_results = Column(JSONB, nullable=True)
    # pending, running, completed, skipped, failed, unsupported.
    # 'unsupported' = terminal quarantine: the runtime cannot see the device in
    # this environment (e.g. Intel GPU under WSL2 — SYCL 0 devices, no /dev/dri).
    # Distinct from 'failed' (transient/real error, bounded-retriable).
    benchmark_status = Column(String(16), default="pending", nullable=False)
    benchmarked_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)

    last_strategy_id = Column(Integer, ForeignKey("accelerator_benchmark_strategies.id"), nullable=True)
    benchmark_hmac_key = Column(String(128), nullable=True)

    # Integrated GPU detection — shared-memory devices consume system RAM
    # and contend with CPU for memory bandwidth. The TFT model needs this
    # to avoid treating iGPU compute as "free" and over-scheduling.
    is_integrated = Column(Boolean, default=False, nullable=False)
    shared_ram_usage_gb = Column(Float, nullable=True)
    cpu_bw_contention = Column(Float, nullable=True)

    # Operator quarantine — an admin one-click "disable this accelerator" flag,
    # orthogonal to benchmark_status (which records the last benchmark OUTCOME).
    # Kept separate so disabling a device that benchmarked fine doesn't destroy
    # its scores, and re-enabling it doesn't force a re-benchmark. When True the
    # device is excluded from scheduling exactly like a failed/unsupported one.
    admin_disabled = Column(Boolean, default=False, nullable=False, server_default="false")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("hardware_profile_id", "kind", "index", name="uq_accel_profile_device"),
        Index("ix_accel_kind_vendor_device", "kind", "vendor_id", "device_id"),
    )


class AcceleratorBenchmarkStrategy(SoftDeleteMixin, Base):
    """Strategy catalog — one per (kind, vendor, device family).

    Seeded rows are curated and ship with the platform (cuda/rocm/intel/npu).
    New rows arrive via LLM generation when the system encounters an
    accelerator kind it hasn't seen before. Strategies carry the Docker image
    ref, optional inline script, and an expected JSON output schema.
    """
    __tablename__ = "accelerator_benchmark_strategies"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(16), nullable=False, index=True)
    vendor_id = Column(String(8), nullable=True)
    device_id_pattern = Column(String(32), nullable=True)
    device_family = Column(String(64), nullable=True)
    image_ref = Column(String(255), nullable=False)
    script_url = Column(String(255), nullable=True)
    script_content = Column(Text, nullable=True)
    env_template = Column(JSONB, nullable=True)
    expected_schema = Column(JSONB, nullable=True)
    source = Column(String(16), nullable=False, default="seeded")
    auto_approved = Column(Boolean, default=False, nullable=False)
    approved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    runs_succeeded = Column(Integer, default=0, nullable=False)
    runs_failed = Column(Integer, default=0, nullable=False)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_anomaly_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("kind", "vendor_id", "device_id_pattern", name="uq_strategy_kind_vendor_device"),
    )


class WorkerProvisioningJobStatus(str, enum.Enum):
    """Status of a worker provisioning job."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerProvisioningJob(Base):
    """
    Tracks worker provisioning jobs (Ansible-based remote setup).
    """
    __tablename__ = "worker_provisioning_jobs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    server_ip = Column(String(255), nullable=False)
    ssh_user = Column(String(100), nullable=True)
    owner_type = Column(Enum(WorkerOwnerType), nullable=False)
    owner_tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    assigned_tenant_ids = Column(SafeARRAY, nullable=True)
    status = Column(Enum(WorkerProvisioningJobStatus), default=WorkerProvisioningJobStatus.PENDING, nullable=False, index=True)
    current_task = Column(String(255), nullable=True)
    error_message = Column(Text, nullable=True)
    ansible_log = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    tenant = relationship("Tenant", foreign_keys=[tenant_id])
    user = relationship("User", foreign_keys=[user_id])
    owner_tenant = relationship("Tenant", foreign_keys=[owner_tenant_id])


class WorkerAssignmentHistoryRecord(Base):
    """
    Audit log for worker assignment changes (assign/unassign/reassign).
    """
    __tablename__ = "worker_assignment_history"

    id = Column(Integer, primary_key=True, index=True)
    worker_license_id = Column(Integer, ForeignKey("worker_licenses.id"), nullable=False, index=True)
    changed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(50), nullable=False)
    old_assigned_tenant_ids = Column(SafeARRAY, nullable=True)
    new_assigned_tenant_ids = Column(SafeARRAY, nullable=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    worker_license = relationship("WorkerLicense", foreign_keys=[worker_license_id])
    changed_by = relationship("User", foreign_keys=[changed_by_user_id])


class WorkerEnrollmentToken(Base):
    """
    Pre-shared enrollment tokens that gate worker enrollment.

    Manual flow:  admin creates token (max_uses=1, auto_approve=False) → shares with operator
    Cloud burst:  system creates token (max_uses=50, auto_approve=True) → passes as user-data
    """
    __tablename__ = "worker_enrollment_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String(255), unique=True, nullable=False, index=True)
    label = Column(String(255), nullable=False)

    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by_system = Column(Boolean, default=False, nullable=False)

    max_uses = Column(Integer, nullable=True)
    use_count = Column(Integer, default=0, nullable=False)

    auto_approve = Column(Boolean, default=False, nullable=False)
    auto_license = Column(Boolean, default=False, nullable=False)

    allowed_name_pattern = Column(String(255), nullable=True)

    # dual-anchor-mesh-federation §6.2 — which control plane a worker enrolled
    # with this token homes to: "local" (site anchor on the manager; survives a
    # WAN cut) or "remote" (fixed cloud anchor; roams). Drives which Headscale
    # plane the preauth key is minted against. Defaults to "local" so on-site is
    # the safe default and pre-migration rows read back as local.
    home_anchor = Column(String(16), default="local", nullable=False)

    owner_type = Column(Enum(WorkerOwnerType), default=WorkerOwnerType.PLATFORM_POOL, nullable=False)
    assigned_tenant_ids = Column(SafeARRAY, nullable=True)
    owner_tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)

    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    created_by = relationship("User", foreign_keys=[created_by_user_id])
    owner_tenant = relationship("Tenant", foreign_keys=[owner_tenant_id])


class WorkerEnrollmentStatus(str, enum.Enum):
    """Enrollment state for out-of-band worker approval."""
    PENDING = "pending"
    APPROVED = "approved"
    REVOKED = "revoked"


class WorkerEnrollment(Base):
    """
    Worker enrollment records used to gate credential issuance.

    A worker registers (PENDING), an admin approves (APPROVED), and credentials
    can be revoked at any time (REVOKED).
    """
    __tablename__ = "worker_enrollments"

    id = Column(Integer, primary_key=True, index=True)

    # Identifiers
    worker_name = Column(String(255), unique=True, nullable=False, index=True)
    hostname = Column(String(255), nullable=True, index=True)

    # Enrollment secrets (store only hashes)
    bootstrap_token_hash = Column(String(255), nullable=False)
    approval_code = Column(String(32), nullable=False, index=True)

    # Which enrollment token was used (NULL for legacy enrollments before token system)
    enrollment_token_id = Column(Integer, ForeignKey("worker_enrollment_tokens.id"), nullable=True)

    # Optional fingerprinting metadata (best-effort; do not treat as strong attestation)
    fingerprint = Column(JSONB, nullable=True)

    status = Column(Enum(WorkerEnrollmentStatus), default=WorkerEnrollmentStatus.PENDING, nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    last_seen_at = Column(DateTime(timezone=True), nullable=True)

    # Token expiry -- workers must re-enroll after this date (default 90 days from approval)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    # BUG-1029-worker-hard-delete (ISO 27001 no-hard-delete): DELETE /workers/{name}
    # tombstones the enrollment row instead of reaping it.
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    deleted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    approved_by = relationship("User", foreign_keys=[approved_by_user_id])
    revoked_by = relationship("User", foreign_keys=[revoked_by_user_id])
    deleted_by = relationship("User", foreign_keys=[deleted_by_user_id])


class NodeLockdownVerdict(SoftDeleteMixin, Base):
    """GSEC FX.115 p2 — the worker's per-node-container lockdown verdict.

    One row per (node kind, flag): the LATEST verdict any worker reported for
    that pair, from the task result's `lockdown_report` field (the Go worker's
    node_lockdown.go planMap — the machine-readable twin of its "lockdown-report"
    log line). This is what the platform-admin report endpoint reads to answer
    "what WOULD break / change if I flip flag X to enforce", so the owner can
    flip a worker.node_lockdown_* knob from evidence instead of from a log
    scrape, and so "already hardened" (missing=false) is visibly distinct from
    "deliberately exempt" (exempt=true + reason).

    Latest-wins, not a ledger: the audit trail of every verdict still lives on
    the task rows themselves (result_payload.lockdown_report), so this table is
    only the aggregation key the report endpoint needs. ISO 27001: no hard
    delete — a flag a node stops reporting keeps its last row with
    `last_seen_at` aged, and an exempted node's row records the reason.

    Idempotent key: (node_kind, flag) is unique among live rows so repeated
    runs overwrite by way of a single live row (services/node_lockdown_report.py
    updates in place rather than minting a row per task).
    """
    __tablename__ = "node_lockdown_verdicts"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The plan's subject: transport.TaskPayload.node_kind, trimmed (the worker
    # trims before reporting, so the key spelling matches the setting's).
    node_kind = Column(String(250), nullable=False, index=True)
    # One of the eight lockdown flags the layer reports (nonroot / capdrop /
    # noprivs / seccomp / rootfs / limits_pids / limits_memory / limits_cpu),
    # plus "scratch_tmpfs" for the writable-scratch set the rootfs knob adds.
    flag = Column(String(32), nullable=False, index=True)
    # The mode that was in effect when this verdict was taken (off|report|enforce)
    # — i.e. which setting the worker had when it ran, not what it would do now.
    mode = Column(String(16), nullable=False)
    # The verdict itself: does the container's current posture LACK the flag,
    # would enforcing it actually CHANGE anything, and did the layer write it.
    missing = Column(Boolean, nullable=False)
    would_apply = Column(Boolean, nullable=False)
    applied = Column(Boolean, nullable=False)
    # Deliberate exemption (worker.node_lockdown_exempt_nodes until FX.115 p3's
    # per-node exception row lands) — with the reason, so the report never
    # conflates "exempt" with "already hardened".
    exempt = Column(Boolean, nullable=False, default=False)
    exempt_reason = Column(String(64), nullable=True)
    # The values the layer requested for this flag (0 = no floor requested),
    # echoed whether or not they were applied, so the report names the number
    # the owner would be shipping.
    cap_value = Column(BigInteger, nullable=True)
    # Extra per-flag detail (seccomp profile name, scratch tmpfs paths, the
    # uid the nonroot knob would drop to) — never parsed by the aggregation.
    detail = Column(JSONB, nullable=True)
    # The worker that reported it, so a fleet can see per-box posture drift.
    worker_name = Column(String(255), nullable=True, index=True)
    task_id = Column(Integer, nullable=True)  # the ApiTaskQueue row this came from
    last_seen_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "uq_node_lockdown_verdict_live_kind_flag",
            "node_kind", "flag",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )


class ApiTaskStatus(str, enum.Enum):
    """Status of a task in the worker task queue."""
    PENDING = "pending"       # Waiting for scheduler to assign
    ASSIGNED = "assigned"     # Scheduler assigned to a worker, not yet claimed
    CLAIMED = "claimed"       # Worker has claimed, not yet started
    RUNNING = "running"       # Worker is actively executing
    COMPLETED = "completed"   # Worker reported success
    FAILED = "failed"         # Worker reported failure or max retries exceeded
    EXPIRED = "expired"       # Claim timed out without completion
    CANCELLED = "cancelled"   # User cancelled the workflow run


class ApiTaskQueue(Base):
    """
    Central task queue for all workers (embedded, datacenter, and remote).

    Tasks are enqueued by Traffic_Cop_Dag.  The OverlapScheduler assigns tasks to
    specific workers with phase plans and start-after timestamps computed from
    multi-resource timeline analysis.  Workers claim assigned tasks, validate each
    phase against actual resources, and report telemetry back.

    Uses PostgreSQL FOR UPDATE SKIP LOCKED for atomic claiming (no double-processing).
    """
    __tablename__ = "api_task_queue"

    id = Column(Integer, primary_key=True, index=True)

    # Link to workflow system
    workflow_run_id = Column(Integer, ForeignKey("workflow_runs.id"), nullable=True, index=True)  # Nullable for non-workflow tasks (zip downloads)
    node_run_id = Column(Integer, ForeignKey("node_runs.id"), nullable=True, index=True)
    node_id = Column(String(255), nullable=False)

    # Tenant scoping (for authorization filtering)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)

    # Queue routing (CPU, CUDA, ROCm, Intel)
    queue_name = Column(String(50), nullable=False, index=True)
    priority = Column(Integer, default=0, nullable=False)
    task_type = Column(String(20), default="processing", nullable=False)  # "processing" or "pre_pull"
    # AI scheduler — priority class branches the placement algorithm. Mirrors the
    # parent WorkflowRun's priority_class for tasks belonging to a workflow; for
    # standalone tasks (calibration, benchmarks) set explicitly at enqueue time.
    priority_class = Column(String(20), nullable=False, default="normal", server_default="normal")

    # Full task payload -- everything the worker needs to execute
    # {inputs, node_config, container_env, image_digest}
    task_payload = Column(JSONB, nullable=False)

    # Status tracking
    status = Column(Enum(ApiTaskStatus), default=ApiTaskStatus.PENDING, nullable=False, index=True)

    # Scheduler assignment — set when OverlapScheduler places the task
    assigned_to_worker = Column(String(255), ForeignKey("worker_licenses.worker_name"), nullable=True, index=True)
    start_after = Column(DateTime(timezone=True), nullable=True)  # When to begin the processing phase
    phase_plan = Column(JSONB, nullable=True)  # Full lifecycle plan with per-phase timestamps + resource impacts
    scheduling_metadata = Column(JSONB, nullable=True)  # Prediction details, confidence, expected vs actual
    scheduling_version = Column(Integer, default=0, nullable=False)  # Incremented on each reschedule

    # Claim tracking
    claimed_by_worker = Column(String(255), ForeignKey("worker_licenses.worker_name"), nullable=True, index=True)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    claim_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Multi-region mesh ownership (add-multi-region-mesh) — the manager that
    # currently owns this unit of work; fencing_token is monotonic and bumped on
    # every ownership change so a stale former owner's writes are rejected after
    # a handoff commit. NULL owner + token 0 = today's single-manager behavior.
    owner_manager_id = Column(String(255), ForeignKey("managers.manager_id"), nullable=True, index=True)
    fencing_token = Column(Integer, default=0, nullable=False, server_default="0")

    # Execution tracking
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    result_payload = Column(JSONB, nullable=True)  # Worker outputs + optional log_tail
    error_message = Column(Text, nullable=True)
    # calibration-health-fixtures-and-timeouts §3 — raw stderr of the tool
    # that actually failed (mc / container runtime / the container itself),
    # kept SEPARATE from error_message rather than appended to it. The
    # observed bug: a sidecar's stdout success banner ("Added myminio
    # successfully.") sat in front of the real stderr line inside
    # error_message and got cut off before the diagnostic part — a missing
    # key, a missing bucket, and an auth failure were all indistinguishable
    # without re-running the task. Populated by the worker's failure report
    # (routers/worker_tasks.py's TaskFailedRequest.error_detail); NULL when
    # the failure isn't sidecar-shaped or the worker predates this field.
    error_detail = Column(Text, nullable=True)

    # Retry tracking
    attempt_count = Column(Integer, default=0, nullable=False)
    max_attempts = Column(Integer, default=3, nullable=False)
    output_namespace = Column(String(50), default="attempt_0", nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Unrunnable flag — set by the scheduler's unrunnable_tasks_sweep
    # job when a PENDING task's gpu_variants doesn't intersect any
    # active worker's hardware_tags. The Dashboard System Health tab
    # renders a banner for tenants with unrunnable tasks so they know
    # to add a matching worker (or skip fewer variants when they build
    # the node). Not a terminal status — clears as soon as a matching
    # worker joins and the task claims.
    unrunnable = Column(Boolean, default=False, nullable=False, index=True)
    unrunnable_reason = Column(Text, nullable=True)  # human-readable "why"

    # FX.40 part 1 (owner decision Q22=A) — scheduler-data pseudonymisation.
    # The daily sweep (services/scheduler_pseudonymise.run_pseudonymise_sweep,
    # gated by scheduler.pseudonymise_job_enabled) stamps api_task_queue rows
    # older than scheduler.pseudonymise_after_days with a keyed HMAC pseudonym
    # of the tenant. `tenant_id` itself is NOT nulled here: the column is
    # nullable=False (authorization filtering reads it everywhere), so keeping
    # it is a deliberate part-1 boundary — nulling it is part 2 and needs a
    # nullability change + auth-path rework. No rows are ever deleted (ISO 27001).
    tenant_pseudonym = Column(String(40), nullable=True, index=True)
    pseudonymised_at = Column(DateTime(timezone=True), nullable=True)

    # FX.40 part 2 (owner decision Q22=A) — scheduler-data aggregate roll-up.
    # Set by the daily aggregate sweep (services/scheduler_pseudonymise.
    # run_aggregate_sweep, gated by scheduler.aggregate_job_enabled) when this
    # row's duration/peak statistics have been folded into
    # scheduler_training_aggregates (age > scheduler.aggregate_after_months).
    # Soft mark only: the row is NEVER deleted and tenant_id is NEVER nulled —
    # training reads the aggregate bucket instead of the rolled-up rows.
    aggregated_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Relationships
    workflow_run = relationship("WorkflowRun", foreign_keys=[workflow_run_id])
    tenant = relationship("Tenant", foreign_keys=[tenant_id])
    assigned_worker = relationship("WorkerLicense", foreign_keys=[assigned_to_worker])

    __table_args__ = (
        Index('ix_api_task_queue_poll', 'status', 'queue_name', 'priority', 'created_at'),
        Index('ix_api_task_queue_assigned', 'status', 'assigned_to_worker', 'start_after'),
    )


class SchedulerTrainingAggregate(Base):
    """FX.40 part 2 (owner decision Q22=A) — tenant-free scheduler aggregates.

    The daily aggregate sweep folds api_task_queue rows older than
    `scheduler.aggregate_after_months` (default 24, i.e. the pseudonymised
    rows past the Q22 retention window) into one row per
    (node_kind, queue_name, gpu_model, month) bucket. The table carries NO
    tenant_id, NO tenant_pseudonym, NO file names / prompts — only counts and
    duration/peak-resource statistics, exactly the columns the scheduler
    retrain reads (CORE/ML/dataset.py targets: task_duration_seconds →
    log_duration, per-task max of ram_mb / vram_mb / cpu_percent /
    net_*_mbps → peak_resources, plus the categorical keys node_kind /
    queue_name / gpu_model from the export).

    Idempotent upsert: counts re-derive from the source rows each sweep
    (rows stay marked `aggregated_at` but are still counted, so a re-run
    over the same window yields identical values).
    """
    __tablename__ = "scheduler_training_aggregates"

    id = Column(Integer, primary_key=True, index=True)
    node_kind = Column(String(255), nullable=False, index=True)
    queue_name = Column(String(50), nullable=False)
    gpu_model = Column(String(255), nullable=True, index=True)
    month = Column(String(7), nullable=False)  # "YYYY-MM" of task created_at
    succeeded = Column(Boolean, nullable=False, default=False, server_default="false")

    task_count = Column(Integer, nullable=False, default=0, server_default="0")
    duration_sum_s = Column(Float, nullable=False, default=0.0, server_default="0")
    duration_avg_s = Column(Float, nullable=True)
    duration_min_s = Column(Float, nullable=True)
    duration_max_s = Column(Float, nullable=True)
    duration_p50_s = Column(Float, nullable=True)
    duration_p95_s = Column(Float, nullable=True)

    peak_ram_max_mb = Column(Float, nullable=True)
    peak_ram_avg_mb = Column(Float, nullable=True)
    peak_vram_max_mb = Column(Float, nullable=True)
    peak_vram_avg_mb = Column(Float, nullable=True)
    peak_cpu_max_pct = Column(Float, nullable=True)
    peak_net_max_mbps = Column(Float, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("node_kind", "queue_name", "gpu_model", "month",
                         "succeeded", name="uq_scheduler_training_aggregates_bucket"),
        Index("ix_scheduler_training_agg_month", "month", "node_kind"),
    )


class PersistentStack(Base):
    """A long-lived docker-compose project MOD deploys on a worker and fully
    manages (add-persistent-stacks). The row is the source of truth and the
    directory ("which worker hosts stack X"). `home_zone` makes it cell-ready
    so the multi-region mesh layers underneath without redesign. Additive and
    inert until a stack is created."""
    __tablename__ = "persistent_stacks"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    created_by_user_id = Column(Integer, nullable=True)  # actor provenance for stack creation
    name = Column(String(255), nullable=False)
    compose_spec = Column(JSONB, nullable=False)  # generated + validated compose project
    # Per-service accelerator DECLARATIONS parsed from the compose spec's
    # x-mod.accelerator extension fields: {service: {required, min_vram_gb,
    # count, kind}}. Requirements only — not a placement decision. Which worker
    # satisfies them is evaluated just-in-time at claim against what that
    # worker reports at that moment, the same way gpu_variants works for node
    # tasks. Normalised at write so the claim-time guard reads a fixed shape
    # instead of re-parsing free-form compose.
    accelerator_requirements = Column(JSONB, nullable=True)
    # delegated-stack-authoring §4 — per-service egress posture parsed from the
    # compose spec's x-mod.egress fields: {service: {mode: none|api|hosts,
    # hosts: [...]}}. RECORDED here at create; the worker does not yet enforce it
    # (Group 5). Null = nothing declared, which is legal until the Group 6 flip.
    egress_policy = Column(JSONB, nullable=True)
    # stack-secrets-and-certs §1 — per-service secret REFERENCES parsed from
    # x-mod.secrets: {service: {env: vault-path}}. References only; no secret
    # value is ever stored here. Null = nothing declared.
    secret_refs = Column(JSONB, nullable=True)
    # stack-secrets-and-certs §5 — per-service TLS config parsed from x-mod.tls:
    # {service: {mode: auto|byo, fqdn: <host>, not_after?: <iso>}}. A byo cert+key
    # itself is a secret at the stack's Vault cert path (never here); this column
    # holds only the non-secret declaration plus the parsed byo expiry, so the
    # read side can warn "expires in N days" without touching Vault. Null =
    # nothing declared.
    tls_config = Column(JSONB, nullable=True)
    # 's3_native' (shared MinIO bucket) | 'local_fs' (volume + snapshot/restore)
    storage_class = Column(String(20), nullable=False, default="local_fs", server_default="local_fs")
    storage_refs = Column(JSONB, nullable=True)  # bucket / volume / snapshot references
    target_worker = Column(String(255), ForeignKey("worker_licenses.worker_name"), nullable=True, index=True)
    home_zone = Column(String(100), nullable=True, index=True)  # cell-ready (mesh consumes this)
    # created/deploying/running/migrating/stopped/torn_down/failed
    status = Column(String(20), nullable=False, default="created", server_default="created")
    # Why the stack is in its current status, when that status was reached by
    # something going wrong. Set by the worker-task completion handler
    # (worker_tasks._apply_stack_task_result) from the worker's reported error.
    # Kept OUT of storage_refs deliberately: that field means "where this
    # stack's data lives" and mixing failure state into it makes both harder
    # to reason about. Cleared on a subsequent successful transition.
    status_reason = Column(Text, nullable=True)
    # Set when a teardown was dispatched but its worker task did not confirm
    # success. The stack row still reads torn_down (the operator asked for it),
    # but containers/volumes may survive on the host — this flag is what a
    # secondary cleanup sweep looks for so a failed teardown can't silently
    # strand resources.
    cleanup_pending = Column(Boolean, nullable=False, default=False, server_default="false")
    # Ephemeral-NIM wake-ownership (services/nim_ephemeral.py). TRUE means this
    # on-demand path WOKE the NIM and so must auto-sleep it when idle; FALSE means
    # it was already on (someone deliberately turned it on) and must be LEFT
    # running after a job. Deliberately a DEDICATED column, NOT status_reason —
    # status_reason is worker/reconciler-managed (cleared on every successful
    # transition), so ownership stored there does not survive from wake to
    # sleep-back. Only nim_ephemeral writes this; every other subsystem ignores it.
    nim_wake_owned = Column(Boolean, nullable=False, default=False, server_default="false")
    lb_endpoint = Column(String(512), nullable=True)
    # mod-connect-service-mesh §2 — per-service overlay identity for reachable
    # services: {service: {magicdns_name, node_id, status}}. Additive alongside
    # the legacy single lb_endpoint string (old stacks keep resolving); the
    # MagicDNS name is deterministic (<stack>-<service>), node_id/status are
    # populated at deploy for teardown-revoke and observed reachability.
    mesh_endpoints = Column(JSONB, nullable=True)
    # caddy-stack-ingress §2 — PUBLIC exposure, opt-in per stack (default
    # internal-only). `exposed` is the magic-URL switch: flipping it on gives the
    # stack a MOD-run public front door. `public_hostname` is the computed,
    # wildcard-routed MOD FQDN `t<tenant_id>-<stack_id>.apps.<BASE_DOMAIN>` — keyed
    # on IDs (like the internal mesh name) so it is collision-proof and follows the
    # stack across workers with zero edge config; set when exposed, cleared when
    # not. `custom_domain` is an optional BYO domain the tenant points at us (CNAME
    # to public_hostname). The ingress on-demand-TLS `ask` endpoint authorises a
    # hostname for a certificate ONLY if it maps to an exposed, good-standing stack
    # here — so exposure is the source of truth for what may go public.
    exposed = Column(Boolean, nullable=False, default=False, server_default="false")
    public_hostname = Column(String(255), nullable=True)
    custom_domain = Column(String(255), nullable=True)
    # friendly-stack-fqdn (mesh-native-llm-routing D4) — an optional human-friendly
    # PUBLIC alias (e.g. `app.example.com`) for an exposed stack. It is
    # a caddy edge route to the stack's STABLE internal mesh name, which is
    # unchanged. MOD provisions NO customer DNS: the customer points their own DNS
    # (CNAME/A) at the MOD edge, and the on-demand-TLS `ask` gate then authorises an
    # on-demand per-host cert for it. Nullable; set when exposed, cleared when not.
    friendly_fqdn = Column(String(255), nullable=True)
    quota_bytes = Column(BigInteger, nullable=True)
    # llm-stack-wake-idle — per-stack idle-reclaim override. NULL means
    # "inherit the platform default", which is why neither column has a
    # default: an interactive assistant and a nightly batch model want
    # opposite answers, and one global setting forces the conservative one on
    # everything. A boolean defaulting to FALSE would silently pin every
    # existing stack to "never reclaim", which is not the same as "unset".
    idle_enabled = Column(Boolean, nullable=True)
    idle_after_seconds = Column(Integer, nullable=True)
    # GX.S5 c7 — pinned stacks are NEVER slept or evicted. Unlike idle_enabled
    # (a nullable per-stack override of the platform default), this is a hard
    # floor: TRUE means idle reclaim cannot take this stack, whatever the
    # platform says. Same server_default idiom as cleanup_pending (:8494).
    keep_running = Column(Boolean, nullable=False, default=False, server_default="false")

    # §8.1 — persistent host pin. NULL = unpinned, which is the prior
    # behaviour. Distinct from `to_worker`, which is an argument to one
    # /migrate call and stops meaning anything the moment that move completes:
    # the next rebalancer pass was free to move the stack straight back.
    #
    # A column rather than a storage_refs key because this CONSTRAINS
    # PLACEMENT — the rebalancer filters on it and the UI shows it — and
    # because it has to survive stop/start. Deliberately has no FK: the pin
    # must outlive the worker row being gone, since "pinned to a host that can
    # no longer satisfy it" is a state to report, not a reason to quietly
    # relocate.
    pinned_worker = Column(String(255), nullable=True, index=True)
    pinned_reason = Column(Text, nullable=True)
    pinned_at = Column(DateTime(timezone=True), nullable=True)
    pinned_by_user_id = Column(Integer, nullable=True)

    # §9.1 — a backup schedule. Backup existed only as POST /{id}/backup, which
    # means it existed only for as long as someone remembered to press it; a
    # backup capability that fires when a human remembers is absent on the day
    # it is needed.
    #
    # NULL = no schedule, and that is deliberately DISTINGUISHABLE from zero
    # rather than collapsed into "disabled" (§9.2). "Nobody has decided how
    # often this stack should be backed up" and "someone decided it needs no
    # backups" are different facts and want different UI, and a stack holding a
    # customer's data with nobody having made that decision is the state most
    # worth surfacing.
    backup_interval_hours = Column(Integer, nullable=True)
    backup_last_run_at = Column(DateTime(timezone=True), nullable=True)
    # §9.3 — a scheduled backup that failed must be visible ON THE STACK. The
    # existing on-demand path reports failure to the caller who pressed the
    # button; a scheduled one has no caller, so without this its only trace is a
    # log line in whichever uvicorn worker held the scheduler lock.
    backup_last_error = Column(Text, nullable=True)
    backup_last_error_at = Column(DateTime(timezone=True), nullable=True)
    # stack-ha-reconciliation §2.1 — what the operator WANTS, kept separate
    # from `status` (what is happening) and from observation (what is actually
    # running). Without it, "stopped on purpose" and "died" look identical, and
    # a reconciler cannot tell which of the two it should fix. Restarting a
    # stack an operator deliberately stopped is not recovery, it is fighting
    # them.
    #
    # "running" | "stopped". Defaults to running because every stack that
    # existed before this column was, by definition, meant to be running —
    # nothing had yet been able to express otherwise.
    desired_state = Column(String(20), nullable=False, default="running",
                           server_default="running")
    # stack-ha-reconciliation §2.2 — whether re-placing this stack elsewhere is
    # CORRECT, which depends entirely on what the service keeps:
    #
    #   stateless        nothing to move; re-place freely
    #   cache_backed     local data whose loss costs time, not correctness.
    #                    vLLM is this: its HuggingFace weights are a cache, so
    #                    a re-placed stack serves correctly after a cold
    #                    download
    #   snapshot_restore data returns from MinIO with an RPO of the last
    #                    snapshot — re-placement is correct only if that RPO
    #                    is acceptable
    #   external         the app replicates its own data; MOD's only job is
    #                    not running two primaries
    #
    # Defaults to `external` — the one class that is NEVER auto-re-placed. An
    # upgrade must not silently make existing stacks eligible for automatic
    # movement: the platform cannot know what someone else's stack keeps, and
    # guessing wrong moves a database.
    ha_class = Column(String(20), nullable=False, default="external",
                      server_default="external")
    # persistent-stack-billing §2/D7 — the priced reservation vector, snapshotted
    # at deploy from _aggregate_accelerator_demand (GPU) + compose
    # deploy.resources.reservations (cpu/mem): {cpu_cores, mem_gb, gpu:[vram_gb...]}.
    # Captured once at deploy (and re-captured on any redeploy that changes the
    # spec) so the meter prices a fixed, auditable vector and never re-parses
    # free-form compose 288×/day. Null = nothing captured yet (pre-billing stack,
    # or a redeploy has not run since the column was added) — priced as 0.
    reservation_units = Column(JSONB, nullable=True)
    # persistent-stack-billing §2/D2 — the anchor for idempotent metering. Each
    # tick charges units·(now − metered_through_at)·rate and advances this in the
    # SAME transaction as the deduct, so a missed tick self-heals (next tick spans
    # the gap) and a double tick is a no-op (the interval is already consumed).
    # Seeded to now() at deploy/wake (never created_at, so a zeroed interval is
    # never billed); cleared on stop (after a final partial charge). Null = not
    # currently metered (stopped/torn_down), and the sweep skips it.
    metered_through_at = Column(DateTime(timezone=True), nullable=True)
    # GX.S10a — a stack variable was set/rotated through
    # PUT /v1/stacks/{id}/variables/{name} (value lives in Vault) but the
    # change has not yet been pushed to the worker. True = the UI shows the
    # "Redeploy to apply" banner. Cleared by the deploy/migrate completion
    # handler once the new value is live on the host.
    needs_redeploy = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class StackSecretIndex(Base):
    """GX.S10a — DB mirror of the tenant's Vault-backed stack secrets.

    The API's Vault approle can NOT list a tenant's prefix, so the "existing
    secrets" dropdown (GET /v1/stacks/secrets/catalog) is served from here
    instead. Values are NEVER stored — only the path + metadata, and the
    row carries no secret material of any kind.

    Append/soft-delete only (ISO 27001): a rotation or re-pointing stamps
    ``deleted_at`` on the prior live row and inserts a fresh one; nothing is
    ever hard-deleted. The live-path unique index is partial (deleted rows
    keep their path without colliding with a new one).
    """
    __tablename__ = "stack_secret_index"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    # Full Vault KV path (the `vault:` ref shape), e.g.
    # tenants/7/stacks/strata/vars/STRATA_API_KEY. `path` is a SQL reserved
    # word — the migration quotes it; SQLAlchemy quotes identifiers that need
    # it here as well.
    path = Column("path", String(512), nullable=False)
    # Denormalised display columns (derived from the path at write time).
    stack_name = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # ISO soft-delete (see class docstring); never hard-deleted.
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)


class PersistentStackRevision(Base):
    """stack-definition-edit — durable, ordered composition+config revisions per
    persistent stack, mirroring ``NodeSourceRevision`` (node-code-edit-rebuild §1).

    ``PersistentStack.compose_spec`` is the CURRENT composition+config; this
    table is the ordered, append-only, forever-retained HISTORY of how it got
    there, so an edit can always be diffed against or reverted to a prior
    revision without destroying anything (ISO 27001 — no hard deletes; see
    ``iso-no-hard-delete``). There is no delete endpoint for these rows and none
    is planned — a stack's history is small (bounded by how often someone edits
    it, not by request volume) and does not need tier-aware pruning the way
    per-request audit logs do.

    ``revision_no`` 1 is materialized lazily the first time a stack is opened
    for editing (from whatever ``compose_spec`` already holds — create/import/
    wizard/template), exactly like ``NodeSourceRevision`` materializes revision
    1 for a legacy node. Every saved edit appends a new row; nothing already
    written is ever mutated.

    The stack FK is ON DELETE SET NULL (with a denormalized ``stack_name``) so
    a revision survives even if its stack is later torn down and the row
    reaped, matching the node-revision convention.
    """
    __tablename__ = "persistent_stack_revision"

    id = Column(Integer, primary_key=True)
    stack_id = Column(ForeignKey("persistent_stacks.id", ondelete="SET NULL"),
                      nullable=True, index=True)
    stack_name = Column(String(255), nullable=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    revision_no = Column(Integer, nullable=False)
    parent_revision_id = Column(
        ForeignKey("persistent_stack_revision.id", ondelete="SET NULL"), nullable=True)
    origin = Column(String(20), nullable=False, default="edited")  # created|edited|reverted
    author_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Full compose_spec snapshot at this revision — the same JSONB shape as
    # PersistentStack.compose_spec (redacted the same way on read).
    compose_spec = Column(JSONB, nullable=False)
    storage_class = Column(String(20), nullable=True)
    # Free-text note the editor can attach to a save ("bumped worker replicas").
    message = Column(Text, nullable=True)
    # 30.6.3 c3-J4c — the credential that carried THIS deploy: the raw
    # `idempotency_key` (so /revert can name and undo a credential-driven deploy)
    # and `key_id` = the deploy credential's ``ApiKey.id`` (same value
    # services/stack_deploy_token.py:132 records as ``api_key_id``). PLAIN Integer,
    # NOT a ForeignKey: a revoked/soft-deleted key must never null or reap a
    # forever-retained revision row (ISO 27001, no hard deletes).
    idempotency_key = Column(String(64), nullable=True, index=True)
    key_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        Index("ix_persistent_stack_revision_stack_rev", "stack_id", "revision_no"),
    )


class StackPackage(Base):
    """GF.8a (launch-completion-megaplan, gf-commerce) — the immutable,
    version-pinned artifact a stack is SOLD as. A stack is a set of images +
    weights + GPU requirements; this row pins one published VERSION of that
    package so a buyer always installs exactly what the publisher tested
    (mirrors the ``WorkflowListing`` node_digest pinning model).

    ``revision_id`` points at the ``PersistentStackRevision`` whose
    ``compose_spec`` was captured when this version was published, so the
    provenance of every stack package is traceable to an exact revision.

    IMMUTABLE ONCE PUBLISHED: a new version is a NEW row, never a mutation.
    ``(slug, version)`` is unique; nothing already published is ever edited.
    ``license_class`` governs distribution: 'redistributable' (MOD may
    redistribute the images/weights), 'byo_key' (customer supplies their own
    API key / license), 'internal' (never sold, platform-internal).
    """
    __tablename__ = "stack_packages"
    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_stack_package_slug_version"),
    )
    id = Column(Integer, primary_key=True)
    slug = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    version = Column(String(64), nullable=False)
    revision_id = Column(ForeignKey("persistent_stack_revision.id", ondelete="SET NULL"),
                         nullable=True, index=True)
    image_digests = Column(JSONB, nullable=True)
    weights = Column(JSONB, nullable=True)
    gpu_requirements = Column(JSONB, nullable=True)
    # GF.10b — persistent home for the frozen build data: the digest-pinned
    # compose spec, the unpinned image tags, and the unfrozen weights list
    # (previously returned by build_package and then lost). Set exactly once
    # at publish time; never mutated afterwards (immutability contract).
    frozen_spec = Column(JSONB, nullable=True)
    license_class = Column(String(16), nullable=False, default="redistributable",
                           server_default="redistributable")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class StackListing(SoftDeleteMixin, Base):
    """GF.8a (launch-completion-megaplan, gf-commerce) — the commercial
    wrapper around a :class:`StackPackage` that makes it catalog-visible,
    mirroring :class:`WorkflowListing` (L2830) column-for-column where it
    makes sense for stacks, so the unified catalog (GG.2) treats every kind
    the same way.

    The package (images/weights/gpu_requirements/version) lives on
    ``StackPackage``; this row carries the sale metadata: price, licence,
    visibility/audience, and the publisher tenant. ``is_listed`` is the
    coarse on/off switch for catalog visibility; ``status`` (draft/published/
    unlisted/archived) gates the fine-grained lifecycle like WorkflowListing.
    ``visibility`` + ``allowed_tenant_ids`` are the GG.6c private/unlisted
    enforcement pair, applied through
    ``services/listing_visibility.apply_list_filter`` everywhere.
    """
    __tablename__ = "stack_listings"
    id = Column(Integer, primary_key=True)
    package_id = Column(ForeignKey("stack_packages.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    publisher_tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                                 nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    price_cents = Column(Integer, nullable=True)
    price_tier = Column(String(20), nullable=True)
    licence = Column(String(20), nullable=False, default="open", server_default="open")
    status = Column(String(20), nullable=False, default="published", server_default="published")
    # GG.6c — 'public' (default) | 'private' | 'unlisted'.
    visibility = Column(String(20), nullable=False, default="public", server_default="public")
    allowed_tenant_ids = Column(JSONB, nullable=False, default=list, server_default="[]")
    is_listed = Column(Boolean, nullable=False, default=True, server_default="true")
    # GSEC FX.95 p1 (owner decision Q81=A) — marketplace listing review
    # state machine (services/listing_review.py), mirroring
    # WorkflowListing's review columns column-for-column. NULL = legacy
    # row published before the review gate existed; the migration
    # backfills 'live' for every already-published row.
    review_status = Column(String(20), nullable=True)
    reviewed_by = Column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    rejection_reason = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    # GSEC FX.95 p2 — auto-check pipeline results (malware/vuln/
    # licence/egress-sandbox; services/listing_review_checks.py).
    # [{name, status: pass|fail|warn|skipped, detail}, ...] at the time
    # of the last pipeline run. A fail does NOT reject — the listing
    # stays in the staff queue flagged.
    check_results = Column(JSONB, nullable=True)
    checks_ran_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)


class TenantStackEntitlement(SoftDeleteMixin, Base):
    """GF.8a (launch-completion-megaplan, gf-commerce) — record of a tenant
    owning a published :class:`StackListing`, mirroring
    :class:`TenantWorkflowEntitlement` (L2883) exactly so the catalog and the
    entitlement_guard (GG.4) treat stacks the same as workflows.

    A single ACTIVE entitlement per (tenant, listing) is enforced by a partial
    unique index over the live rows (``deleted_at IS NULL``), so a soft-deleted
    (revoked) entitlement does not block re-granting the same listing later.
    ``source`` reuses the purchase/plan_grant/admin_grant convention and stays
    a plain String (not an enum) so it can be widened without a migration.
    ``expires_at`` is the soft-revocation marker (set + past => not entitled),
    same convention as TenantWorkflowEntitlement.
    """
    __tablename__ = "tenant_stack_entitlements"
    __table_args__ = (
        Index(
            "uq_tenant_stack_entitlement_live",
            "tenant_id", "listing_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    listing_id = Column(ForeignKey("stack_listings.id", ondelete="CASCADE"), nullable=False, index=True)
    granted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    granted_by_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    source = Column(String(20), nullable=False, default="admin_grant", server_default="admin_grant")
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # GF.12 — the PersistentStack materialized for the tenant by the catalog
    # install flow (services/stack_install_service). ON DELETE SET NULL so a
    # reaped stack row leaves the entitlement intact and install idempotently
    # re-runnable (a fresh stack is created on the next install).
    installed_stack_id = Column(ForeignKey("persistent_stacks.id", ondelete="SET NULL"),
                                nullable=True, index=True)


class LlmUsage(Base):
    """Last time each managed LLM endpoint was actually asked for something.

    llm-stack-wake-idle §1. The input to idle reclamation, and deliberately
    recorded here rather than at the gateway: `utils/llm_client.py` contains a
    single `requests.post` that all 43 of its importers pass through, so the
    API is already the one client for every managed LLM call. Measuring at the
    gateway would have put a dependency-free resource win behind
    `managed-llm-fqdn-gateways` (0 of 44 tasks done).

    Keyed by (tool, base_url, model) rather than by stack, because a stack is
    not knowable from here — the client only knows the endpoint it called.
    Idleness is resolved the other way round: the reclaimer maps a stack to its
    endpoint and asks when anything last used it.

    Both axes are needed. Idleness is per-ENDPOINT — a stack is idle when NO
    tool has called it. Policy is per-TOOL — an interactive assistant may
    require warm capacity where a nightly batch does not. Recording only the
    endpoint would lose the ability to say "keep this up because the assistant
    uses it".
    """
    __tablename__ = "llm_usage"

    id = Column(Integer, primary_key=True, index=True)
    # The calling tool, from llm_client's `_label` — "wizard", "repair", etc.
    tool = Column(String(64), nullable=False, index=True)
    base_url = Column(String(512), nullable=False)
    model = Column(String(255), nullable=False)
    # Set when a request STARTS. Crediting at completion would make a model
    # that is mid-generation look untouched for the length of its own longest
    # request, which is the opposite of idle.
    last_used_at = Column(DateTime(timezone=True), server_default=func.now(),
                          nullable=False, index=True)
    call_count = Column(BigInteger, nullable=False, default=0, server_default="0")

    __table_args__ = (
        UniqueConstraint("tool", "base_url", "model", name="uq_llm_usage_key"),
        Index("ix_llm_usage_endpoint", "base_url", "model", "last_used_at"),
    )


class StackObservation(Base):
    """What a worker reports it is ACTUALLY running (stack-ha-reconciliation §1).

    The observed half of reconciliation, and deliberately separate from
    `PersistentStack`. That row records intent and the outcome of the last
    dispatched deploy — neither of which says anything about the hours since.
    Measured on the dev deployment: two vLLM stacks exited 2026-07-28 and
    `persistent_stacks.status` still read `running` two days later, holding 15
    of 16 GB of VRAM out of the node-task pool the whole time, because nothing
    in the system ever asked a worker what it was running.

    Keyed by (worker_name, project) rather than by stack_id because the table
    must be able to express things no stack row claims. An orphan project — one
    running on a worker that no stack expects — is a real condition worth
    surfacing (a teardown whose worker task never landed), and it is
    inexpressible in a schema that can only describe known stacks.

    `stack_id` is therefore nullable and resolved by matching the reported
    compose project against `_project_name(stack)`. Matching on the project
    string keeps blue/green migration generations (g0, g1, ...) correct without
    the worker needing to understand generations at all.
    """
    __tablename__ = "stack_observations"

    id = Column(Integer, primary_key=True, index=True)
    worker_name = Column(String(255), nullable=False, index=True)
    # docker compose project name, as reported by the worker.
    project = Column(String(255), nullable=False, index=True)
    # Resolved at ingest where the project matches a known stack; NULL marks an
    # orphan — containers running that nothing expects.
    stack_id = Column(Integer, nullable=True, index=True)
    # Container counts within the project. `running` is what decides whether
    # the stack is serving; `total` distinguishes "this host never had it" from
    # "this host has it and it is down" — different failures, different
    # responses, and indistinguishable from a single boolean.
    running = Column(Integer, nullable=False, default=0, server_default="0")
    total = Column(Integer, nullable=False, default=0, server_default="0")
    observed_at = Column(DateTime(timezone=True), server_default=func.now(),
                         nullable=False, index=True)
    # Last time this project was seen with a RUNNING container here, as
    # distinct from last reported.
    #
    # `observed_at` refreshes on every heartbeat that mentions the project —
    # including one reporting running=0, because the stopped containers still
    # exist and still carry the compose label. So "how long has this been
    # down" is unanswerable from `observed_at` alone: it stays fresh forever
    # while the stack is dead. Measured on the dev box, a stack stopped for 12
    # minutes still reported an observation age of 10 seconds, and the
    # reconciler's gap window could never elapse.
    last_running_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Per-stack GPU memory this project's RUNNING containers are actually holding,
    # in GB, as sampled by the worker each beat (nvidia-smi compute-apps summed
    # over the PIDs the daemon attributes to this project's containers).
    #
    # NULL means UNMEASURED, never "measured zero": no GPU on the host, an older
    # worker binary that does not sample it, or a project with no running
    # containers. It is the ONLY per-stack serving-VRAM signal the platform has,
    # and `stack_calibration.LiveDriver.sample` reads the freshest fresh value
    # here to populate `StackProfile.serving_vram_gb`. Consumers must fall back
    # to the declared VRAM whenever it is NULL.
    observed_vram_gb = Column(Float, nullable=True)
    # LLM-gateway (SMG) queue metrics this project's gateway reported on the
    # beat, parsed from the /workers + /metrics scrape (Group R 27.5a). A
    # snapshot dict: {replicas_total, replicas_healthy, queue_depth,
    # per_worker:[{url,healthy,active}], wait_p50_s, wait_p95_s,
    # cache_hit_rate}. NULL until a gateway-fronted stack first reports one —
    # non-LLM stacks never populate it.
    gateway_metrics = Column(JSONB, nullable=True)
    # GA.ASW.4 — per-service compose state this beat, when the worker reported
    # one: [{"name": <service>, "state": <docker state>, "health": <compose
    # healthcheck status>}, ...]. `state` is the daemon's word (running,
    # exited, restarting, ...); `health` is the compose healthcheck's word
    # (starting, healthy, unhealthy) and is absent for a service with no
    # healthcheck defined. NULL means the worker did not report per-service
    # detail this beat (older binary, or a project with no health data to
    # give) — `stack_health_grade` falls back to the `running`/`total` counts
    # above rather than reading NULL as "everything is fine".
    service_states = Column(JSONB, nullable=True)
    # GR.FEN.1b — the ha_class label the containers on this host ACTUALLY
    # carry, read from the `com.example.ha-class` label the worker reports
    # each beat as `ha_class` in its stack observation. NULL until a worker
    # that reports it first observes the project (older binary, or a
    # pre-label deploy). This can disagree with PersistentStack.ha_class
    # after the class is edited: the new label only reaches the running
    # containers on a redeploy, and `ha_class_state` surfaces that as a
    # "redeploy needed to apply" mismatch.
    observed_ha_class = Column(String(50), nullable=True)

    __table_args__ = (
        UniqueConstraint("worker_name", "project", name="uq_stack_obs_worker_project"),
        Index("ix_stack_obs_stack_observed", "stack_id", "observed_at"),
    )


class WorkerStackReliabilityDaily(Base):
    """How well stacks actually stayed up on one worker, per day (§8.6).

    The rebalancer used to decide on FREE CAPACITY ALONE, which answers "where
    is there room" and never "does a stack survive up there". A worker with a
    flaky card, a bad driver or a failing PSU looks maximally attractive by that
    measure precisely BECAUSE its stacks keep dying and freeing capacity, so the
    stack gets moved toward the host least able to keep it.

    `stack_observations` cannot answer this. It is upserted one row per
    (worker, project) and the row is DELETED when the project stops being
    reported, so it holds current state, not history: `observed_at` is always
    ~now and a seven-day query returns however many projects are on the host
    right now. Reading it as a time series silently yields a sample of two.

    One row per (worker, day), following the TenantEgressUsageDaily /
    TenantHarborUsageDaily shape. Counters accumulate; nothing is ever
    rewritten, so a pass reads a stable window.

    `stacks_observed` is the DENOMINATOR and holds the PEAK number of stacks
    seen on this host that day, so summing it over a window gives stack-days of
    exposure. Not a per-beat tally: at a 15s heartbeat that would reach ~11,500
    a day for two stacks, and any real failure rate divided by it rounds to
    zero. Without a denominator at all the counters are unusable in the other
    direction — three failures on a host running thirty stacks and three on a
    host running three are the same integer and opposite facts.
    """
    __tablename__ = "worker_stack_reliability_daily"

    id = Column(Integer, primary_key=True, index=True)
    worker_name = Column(String(255), nullable=False, index=True)
    day = Column(Date, nullable=False, index=True)

    # A stack that was meant to be running here and stopped being. Recorded on
    # the transition only, not per pass, so one incident counts once.
    down_events = Column(Integer, nullable=False, default=0, server_default="0")
    # A deploy or migrate this worker claimed and failed. Distinct from a
    # down_event: the stack never came up at all, which implicates the host's
    # ability to start work rather than its ability to sustain it.
    deploy_failures = Column(Integer, nullable=False, default=0, server_default="0")
    # Denominator: PEAK stacks seen on this host today. Summed over a window it
    # is stack-days of exposure. Written with max(), not +=.
    stacks_observed = Column(Integer, nullable=False, default=0, server_default="0")

    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("worker_name", "day",
                         name="uq_worker_stack_reliability_worker_day"),
    )


class ResourcePattern(Base):
    """
    Historical resource usage patterns per node category.

    Populated by the pattern_updater after each task completion.  The OverlapScheduler
    reads these to predict future resource curves and compute overlap windows.
    Replaces the ad-hoc query of cogs.processing_metadata for pattern data.
    """
    __tablename__ = "resource_patterns"

    id = Column(Integer, primary_key=True, index=True)

    # Pattern identity — one pattern per (node_kind, queue_name, size_tier) tuple
    node_kind = Column(String(255), nullable=False, index=True)
    queue_name = Column(String(50), nullable=False)
    size_tier = Column(String(20), default="any", nullable=False)  # small (<100MB), medium (100MB-2GB), large (>2GB), any
    gpu_model = Column(String(100), nullable=True, index=True)  # #92: GPU-type-aware patterns (e.g., "RTX 4090")

    # Pattern classification from pattern_recognition.py
    pattern_type = Column(String(50), nullable=False)  # linear_ramp, step_function, plateau, sawtooth, declining, unknown
    pattern_data = Column(JSONB, nullable=False)  # Full classify_resource_pattern() output with metrics

    # Per-phase resource profiles (entire lifecycle, not just processing)
    # {image_pull: {avg_duration_s, avg_network_mbps, ...}, processing: {pattern, ...}, ...}
    phase_profiles = Column(JSONB, nullable=True)

    # Aggregate statistics across all contributing executions
    sample_count = Column(Integer, default=0, nullable=False)  # Number of executions that contributed
    avg_duration_seconds = Column(Float, nullable=True)
    avg_peak_ram_mb = Column(Float, nullable=True)
    avg_peak_vram_mb = Column(Float, nullable=True)
    avg_input_size_mb = Column(Float, nullable=True)
    avg_output_size_mb = Column(Float, nullable=True)
    confidence = Column(Float, default=0.0, nullable=False)  # Aggregate confidence (0-1)

    last_updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index('ix_resource_pattern_node_kind', 'node_kind', 'queue_name', 'size_tier', unique=True),
    )


class WorkerResourceTimeline(Base):
    """
    Real-time resource snapshot per worker, updated every heartbeat (15s).

    The OverlapScheduler reads these to build projected multi-resource timelines
    when computing task placements.  One row per worker, always the latest state.
    """
    __tablename__ = "worker_resource_timelines"

    id = Column(Integer, primary_key=True, index=True)
    worker_name = Column(String(255), ForeignKey("worker_licenses.worker_name"), nullable=False, unique=True, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # System resources
    cpu_percent = Column(Float, nullable=True)
    ram_used_mb = Column(Float, nullable=True)
    ram_total_mb = Column(Float, nullable=True)
    ram_available_mb = Column(Float, nullable=True)  # Actual available (accounts for reclaimable cache)

    # GPU
    vram_used_mb = Column(Float, nullable=True)
    vram_total_mb = Column(Float, nullable=True)
    vram_fragmentation_pct = Column(Float, nullable=True)  # 1.0 - (largest_free_block / total_free)
    gpu_utilization_pct = Column(Float, nullable=True)
    gpu_temperature_c = Column(Float, nullable=True)
    gpu_power_state = Column(String(20), nullable=True)  # "active", "idle", "low_power"

    # Network
    nic_rx_mbps = Column(Float, nullable=True)
    nic_tx_mbps = Column(Float, nullable=True)
    nic_capacity_mbps = Column(Float, nullable=True)

    # Disk
    disk_read_iops = Column(Float, nullable=True)
    disk_write_iops = Column(Float, nullable=True)
    disk_free_gb = Column(Float, nullable=True)

    # Docker
    active_containers = Column(Integer, nullable=True)
    containers_starting = Column(Integer, nullable=True)  # Currently in create/start phase

    # Thermal
    cpu_temperature_c = Column(Float, nullable=True)
    thermal_throttled = Column(Boolean, nullable=True)

    # Per-task breakdown: {task_id: {ram_mb, vram_mb, cpu_pct, phase, elapsed_s}}
    task_snapshots = Column(JSONB, nullable=True)

    # Relationship
    worker = relationship("WorkerLicense", foreign_keys=[worker_name])


class TaskResourceSample(Base):
    """
    Time-series resource samples reported by workers during task execution.

    Workers send batched samples every 30s.  After task completion, the
    pattern_updater classifies these into ResourcePattern entries for
    future scheduling decisions.
    """
    __tablename__ = "task_resource_samples"

    id = Column(BigInteger, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("api_task_queue.id"), nullable=False, index=True)
    worker_name = Column(String(255), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    phase = Column(String(50), nullable=False)  # image_pull, data_download, processing, upload, etc.

    # Resource measurements
    cpu_percent = Column(Float, nullable=True)
    ram_mb = Column(Float, nullable=True)
    vram_mb = Column(Float, nullable=True)
    gpu_util_pct = Column(Float, nullable=True)
    gpu_temp_c = Column(Float, nullable=True)
    net_rx_mbps = Column(Float, nullable=True)
    net_tx_mbps = Column(Float, nullable=True)
    disk_read_iops = Column(Float, nullable=True)
    disk_write_iops = Column(Float, nullable=True)

    # Extended measurements (v2)
    single_thread_detected = Column(Boolean, nullable=True)
    container_disk_read_mb = Column(Float, nullable=True)
    container_disk_write_mb = Column(Float, nullable=True)
    vram_alloc_rate_mb_s = Column(Float, nullable=True)
    process_count = Column(Integer, nullable=True)
    gpu_clock_mhz = Column(Float, nullable=True)
    swap_used_mb = Column(Float, nullable=True)

    # Relationship
    task = relationship("ApiTaskQueue", foreign_keys=[task_id])

    __table_args__ = (
        Index('ix_task_samples_task_ts', 'task_id', 'timestamp'),
    )


class PredictionLog(Base):
    """Logs every ML scheduling prediction for accuracy tracking.

    Predictions are logged at scheduling time. Actual values backfilled on
    task completion. Used for shadow mode comparison, auto-retrain triggers,
    and continuous accuracy monitoring.
    """
    __tablename__ = "prediction_logs"

    id = Column(BigInteger, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("api_task_queue.id"), nullable=False, index=True)
    model_version = Column(Integer, nullable=False)
    inference_mode = Column(String(10), nullable=False)  # cold, warm
    predicted_duration_s = Column(Float)
    predicted_pattern = Column(String(30))
    predicted_peak_ram_mb = Column(Float)
    predicted_peak_vram_mb = Column(Float)
    confidence = Column(Float)
    actual_duration_s = Column(Float, nullable=True)
    actual_pattern = Column(String(30), nullable=True)
    prediction_error_pct = Column(Float, nullable=True)
    predicted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    task = relationship("ApiTaskQueue", foreign_keys=[task_id])

    __table_args__ = (
        Index('ix_prediction_log_task', 'task_id'),
        Index('ix_prediction_log_version', 'model_version'),
    )


class WorkerSystemState(Base):
    """Worker OS / package / air-gap telemetry for calibration staleness.

    1:1 with WorkerLicense. Heartbeat-populated. The staleness evaluator
    compares current values against the baseline_* snapshot taken at the
    last calibration completion to decide whether the parent HardwareProfile
    needs recalibration. air_gapped is reported by the worker itself
    (env var WORKER_AIR_GAPPED) — declaration not detection — and exempts
    the profile from the package-delta ratchet.
    """
    __tablename__ = "worker_system_state"

    id = Column(Integer, primary_key=True, index=True)
    worker_license_id = Column(Integer, ForeignKey("worker_licenses.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    kernel_version = Column(String(128), nullable=True)
    os_distro = Column(String(64), nullable=True)
    os_version = Column(String(64), nullable=True)
    package_count = Column(Integer, nullable=False, default=0, server_default="0")
    air_gapped = Column(Boolean, nullable=False, default=False, server_default="false")

    baseline_kernel_version = Column(String(128), nullable=True)
    baseline_os_version = Column(String(64), nullable=True)
    baseline_package_count = Column(Integer, nullable=True)
    last_baselined_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    worker_license = relationship("WorkerLicense", foreign_keys=[worker_license_id])


class WorkerNoahMode(Base):
    """NOAH lend/pause pin — 24.5.s2 (no-large-sweep): "lend this machine" /
    "pause lending (I'm using it)", optionally "until <time>".


    1:1 with WorkerLicense. This is the SERVER-side twin of the worker's local
    pin (CORE/Worker/internal/availability/local_override.go, POST /noah/mode):
    the same three-word vocabulary (lend | pause | none) and the same verdict
    shape, so the Dashboard control and the machine's own tray switch are two
    entry points into ONE decision, not two availability machines.

    mode NULL = no pin: the automatic idle-detection verdict rules (24.5.s1).
    until set + mode pause = "pause lending until 18:00" — the pin is released
    by the API once now >= until (it pushes "none" back), so a desktop owner
    can say "I'm using it for the next hour" without a second click.

    Append-only companion: every set is audited (models.AuditEvent,
    NOAH_MODE_SET/NOAH_MODE_CLEARED) like WORKER_PAUSED/WORKER_RESUMED.
    """
    __tablename__ = "worker_noah_mode"
    __table_args__ = (
        CheckConstraint(
            "mode IS NULL OR mode IN ('lend', 'pause', 'none')",
            name="ck_worker_noah_mode_vocab",
        ),
        UniqueConstraint("worker_license_id", name="uq_worker_noah_mode_1to1"),
    )

    id = Column(Integer, primary_key=True)
    worker_license_id = Column(
        Integer,
        ForeignKey("worker_licenses.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    mode = Column(String(8), nullable=True)  # lend | pause | none; NULL = automatic
    until = Column(DateTime(timezone=True), nullable=True)  # only meaningful with pause
    set_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    set_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    worker_license = relationship("WorkerLicense", foreign_keys=[worker_license_id])


class PredictionTrainingMarker(Base):
    """Singleton row tracking auto-retrain trigger state.

    Increments recalibrations_since_last_train every time a HardwareProfile
    completes recalibration. Auto-retrain fires when the counter hits the
    configured threshold (default 3) OR when (now - last_train_at) exceeds
    the safety-net interval (default 90 days). Reset to 0 on successful
    retrain promotion.
    """
    __tablename__ = "prediction_training_marker"

    id = Column(Integer, primary_key=True)
    last_train_at = Column(DateTime(timezone=True), nullable=True)
    last_train_model_version = Column(Integer, nullable=True)
    recalibrations_since_last_train = Column(Integer, nullable=False, default=0, server_default="0")
    last_check_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ExperimentStatus(str, enum.Enum):
    """Status of a pricing experiment."""
    DRAFT = "draft"              # Experiment created but not started
    ACTIVE = "active"            # Experiment is running
    PAUSED = "paused"            # Temporarily paused
    COMPLETED = "completed"      # Experiment finished successfully
    CANCELLED = "cancelled"      # Experiment cancelled before completion


class PricingExperiment(Base):
    """
    A/B test experiment for testing different pricing strategies.

    Allows platform admins to:
    - Test price changes on a subset of tenants before full rollout
    - Compare multiple pricing strategies simultaneously
    - Measure impact on revenue, conversion, and churn
    - Make data-driven pricing decisions

    Workflow:
    1. Create experiment with multiple variants
    2. Define sample size and allocation strategy
    3. Activate experiment to assign tenants to variants
    4. Collect metrics during experiment period
    5. Analyze results and choose winning variant
    6. Roll out winning variant to all tenants
    """
    __tablename__ = "pricing_experiments"

    id = Column(Integer, primary_key=True, index=True)

    # Experiment details
    name = Column(String(200), nullable=False)  # "Q4 2024 Price Increase Test"
    description = Column(Text, nullable=True)  # Detailed experiment goals and hypothesis
    hypothesis = Column(Text, nullable=True)  # "Increasing price by 10% will increase revenue without significant churn"

    # Experiment status and timeline
    status = Column(Enum(ExperimentStatus), default=ExperimentStatus.DRAFT, nullable=False, index=True)
    start_date = Column(DateTime(timezone=True), nullable=True, index=True)
    end_date = Column(DateTime(timezone=True), nullable=True, index=True)
    actual_start_date = Column(DateTime(timezone=True), nullable=True)  # When experiment actually started
    actual_end_date = Column(DateTime(timezone=True), nullable=True)  # When experiment actually ended

    # Lifecycle timestamps written by start/stop/end (services/admin_plans_ab_test_service.py).
    # Distinct from actual_start_date/actual_end_date above: those predate this
    # service and are not consistently set by it, and paused_at has no
    # equivalent among the pre-existing columns (an experiment can be
    # paused and later ended, resumed, or paused again).
    started_at = Column(DateTime(timezone=True), nullable=True)
    paused_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Sample configuration
    sample_size_percent = Column(Integer, default=10, nullable=False)  # % of total tenants to include (1-100)
    min_sample_size = Column(Integer, default=10, nullable=False)  # Minimum number of tenants required
    max_sample_size = Column(Integer, nullable=True)  # Maximum number of tenants (null = no limit)

    # Targeting criteria (JSONB for flexibility)
    # Examples: {"billing_mode": "postpaid"}, {"monthly_revenue_min": 100}, {"plan_tier": "pro"}
    targeting_criteria = Column(JSONB, nullable=True)

    # Assignment strategy
    assignment_method = Column(String(50), default="random", nullable=False)  # "random", "stratified", "manual"
    stratification_key = Column(String(100), nullable=True)  # Field to stratify on (e.g., "monthly_revenue_bucket")

    # Control variant
    control_variant_id = Column(Integer, ForeignKey("pricing_variants.id"), nullable=True)

    # Results and analysis
    winner_variant_id = Column(Integer, ForeignKey("pricing_variants.id"), nullable=True)
    confidence_level = Column(Integer, nullable=True)  # Statistical confidence % (e.g., 95)
    results_summary = Column(JSONB, nullable=True)  # Detailed results and analysis

    # Creator and reviewers
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reviewed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    created_by = relationship("User", foreign_keys=[created_by_user_id])
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_user_id])
    control_variant = relationship("PricingVariant", foreign_keys=[control_variant_id], post_update=True)
    winner_variant = relationship("PricingVariant", foreign_keys=[winner_variant_id], post_update=True)
    variants = relationship("PricingVariant", back_populates="experiment", foreign_keys="[PricingVariant.experiment_id]")
    assignments = relationship("TenantExperimentAssignment", back_populates="experiment", cascade="all, delete-orphan")
    metrics = relationship("ExperimentMetric", back_populates="experiment", cascade="all, delete-orphan")


class PricingVariant(Base):
    """
    A pricing variant within an A/B test experiment.

    Each experiment has 2+ variants (including a control).
    Variants define different pricing configurations to test.
    """
    __tablename__ = "pricing_variants"

    id = Column(Integer, primary_key=True, index=True)
    experiment_id = Column(Integer, ForeignKey("pricing_experiments.id"), nullable=False, index=True)

    # Variant details
    name = Column(String(100), nullable=False)  # "Control", "10% Increase", "Volume Discount"
    description = Column(Text, nullable=True)
    is_control = Column(Boolean, default=False, nullable=False)

    # Pricing configuration
    postpaid_price_cents = Column(Integer, nullable=True)  # Price for postpaid users
    overage_price_cents = Column(Integer, nullable=True)  # Price for subscription overages

    # Volume discount tiers (optional)
    tier1_threshold = Column(Integer, nullable=True)
    tier1_price_cents = Column(Integer, nullable=True)
    tier2_threshold = Column(Integer, nullable=True)
    tier2_price_cents = Column(Integer, nullable=True)
    tier3_price_cents = Column(Integer, nullable=True)

    # Allocation
    allocation_percent = Column(Integer, nullable=False)  # % of experiment participants assigned to this variant
    actual_allocation_count = Column(Integer, default=0, nullable=False)  # Actual number of tenants assigned

    # Performance tracking
    total_revenue_cents = Column(Integer, default=0, nullable=False)  # Total revenue from this variant
    total_conversions = Column(Integer, default=0, nullable=False)  # Number of conversions (new purchases)
    total_churns = Column(Integer, default=0, nullable=False)  # Number of cancellations

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    experiment = relationship("PricingExperiment", back_populates="variants", foreign_keys=[experiment_id])
    assignments = relationship("TenantExperimentAssignment", back_populates="variant", cascade="all, delete-orphan")
    metrics = relationship("ExperimentMetric", back_populates="variant", cascade="all, delete-orphan")

    # Constraint: Variant name must be unique within an experiment
    __table_args__ = (
        UniqueConstraint("experiment_id", "name", name="uq_experiment_variant_name"),
    )


class TenantExperimentAssignment(Base):
    """
    Assigns a tenant to a specific variant in a pricing experiment.

    Each tenant can only be in one variant per experiment.
    Assignments are created when experiment starts and remain stable
    throughout the experiment to ensure valid statistical analysis.
    """
    __tablename__ = "tenant_experiment_assignments"

    id = Column(Integer, primary_key=True, index=True)
    experiment_id = Column(Integer, ForeignKey("pricing_experiments.id"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    variant_id = Column(Integer, ForeignKey("pricing_variants.id"), nullable=False, index=True)

    # Assignment details
    assignment_method = Column(String(50), nullable=False)  # "random", "stratified", "manual"
    assigned_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    assigned_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # Null for automatic assignment

    # Stratification metadata (if using stratified assignment)
    stratification_metadata = Column(JSONB, nullable=True)  # {"revenue_bucket": "high", "plan_tier": "pro"}

    # Exclusion tracking
    excluded = Column(Boolean, default=False, nullable=False)  # Tenant excluded from analysis (e.g., churned during experiment)
    excluded_reason = Column(String(500), nullable=True)
    excluded_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    experiment = relationship("PricingExperiment", back_populates="assignments")
    tenant = relationship("Tenant")
    variant = relationship("PricingVariant", back_populates="assignments")
    assigned_by = relationship("User")

    # Constraint: Each tenant can only be assigned once per experiment
    __table_args__ = (
        UniqueConstraint("experiment_id", "tenant_id", name="uq_experiment_tenant_assignment"),
    )


class MetricType(str, enum.Enum):
    """Types of metrics collected during pricing experiments."""
    REVENUE = "revenue"              # Total revenue
    CONVERSION = "conversion"        # New subscription/purchase
    CHURN = "churn"                  # Subscription cancellation
    UPGRADE = "upgrade"              # Plan upgrade
    DOWNGRADE = "downgrade"          # Plan downgrade
    USAGE = "usage"                  # Credit usage
    CUSTOM = "custom"                # Custom metric


class ExperimentMetric(Base):
    """
    Metrics collected during pricing experiments for analysis.

    Tracks various business metrics (revenue, conversions, churn, etc.)
    per variant to enable data-driven decision making.

    Metrics can be:
    - Aggregated (total revenue for variant)
    - Per-tenant (revenue from specific tenant)
    - Time-series (daily revenue)
    """
    __tablename__ = "experiment_metrics"

    id = Column(Integer, primary_key=True, index=True)
    experiment_id = Column(Integer, ForeignKey("pricing_experiments.id"), nullable=False, index=True)
    variant_id = Column(Integer, ForeignKey("pricing_variants.id"), nullable=False, index=True)

    # Metric details
    metric_type = Column(Enum(MetricType), nullable=False, index=True)
    metric_name = Column(String(100), nullable=True)  # For custom metrics
    metric_value = Column(Integer, nullable=False)  # Stored as integer (cents for revenue, count for conversions)

    # Tenant-specific metrics (optional - null for aggregated metrics)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)

    # Time period
    period_start = Column(DateTime(timezone=True), nullable=True)
    period_end = Column(DateTime(timezone=True), nullable=True)

    # Collection metadata
    collected_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    collection_method = Column(String(50), nullable=True)  # "automatic", "manual", "import"

    # Additional context
    metric_metadata = Column(JSONB, nullable=True)

    # Relationships
    experiment = relationship("PricingExperiment", back_populates="metrics")
    variant = relationship("PricingVariant", back_populates="metrics")
    tenant = relationship("Tenant")


class GeographicRegion(SoftDeleteMixin, Base):
    """
    Geographic regions for region-specific pricing.

    Allows defining pricing zones by:
    - Individual countries (US, CA, GB, etc.)
    - Continents or regions (North America, Europe, APAC, etc.)
    - Custom groupings (Tier 1 Countries, Emerging Markets, etc.)

    Use cases:
    - Lower prices for emerging markets
    - Regional pricing adjustments
    - Compliance with regional pricing regulations
    - Competitive pricing per market
    """
    __tablename__ = "geographic_regions"

    id = Column(Integer, primary_key=True, index=True)

    # Region identification
    code = Column(String(50), unique=True, nullable=False, index=True)  # "US", "EU", "APAC", "TIER1"
    name = Column(String(100), nullable=False)  # "United States", "European Union", "Asia Pacific"
    description = Column(Text, nullable=True)  # Detailed description of the region

    # Region type
    region_type = Column(String(20), nullable=False)  # "country", "continent", "custom"

    # Countries included (for multi-country regions)
    # ISO 3166-1 alpha-2 country codes
    country_codes = Column(JSONB, nullable=True)  # ["US", "CA"] for North America

    # Status
    is_active = Column(Boolean, default=True, nullable=False)

    # Display configuration
    display_order = Column(Integer, default=0, nullable=False)  # Order in region selector

    # Metadata
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    created_by = relationship("User")
    regional_pricing = relationship("RegionalPricing", back_populates="region", cascade="all, delete-orphan")


class RegionalPricing(SoftDeleteMixin, Base):
    """
    Region-specific pricing overrides for geographic pricing.

    Allows setting different prices for different geographic regions.
    Overrides global pricing for tenants in the specified region.

    Examples:
    - US region: $0.10/credit (global price)
    - India region: $0.05/credit (50% discount for emerging market)
    - EU region: $0.12/credit (premium pricing for high-income market)
    """
    __tablename__ = "regional_pricing"

    id = Column(Integer, primary_key=True, index=True)
    region_id = Column(Integer, ForeignKey("geographic_regions.id"), nullable=False, index=True)

    # Pricing overrides (null = use global pricing)
    postpaid_price_cents = Column(Integer, nullable=True)  # Override for postpaid pricing
    overage_price_cents = Column(Integer, nullable=True)  # Override for overage pricing

    # Plan-specific overrides (optional)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=True, index=True)  # Null = applies to all plans

    # Pricing adjustment metadata
    price_adjustment_reason = Column(Text, nullable=True)  # "Market competitiveness", "PPP adjustment", etc.
    price_adjustment_percentage = Column(Integer, nullable=True)  # Percentage relative to global price (basis points)

    # Validity period
    effective_from = Column(DateTime(timezone=True), nullable=False, index=True)
    effective_until = Column(DateTime(timezone=True), nullable=True, index=True)  # Null = no expiration

    # Status
    is_active = Column(Boolean, default=True, nullable=False)

    # Metadata
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    region = relationship("GeographicRegion", back_populates="regional_pricing")
    plan = relationship("Plan")
    created_by = relationship("User")

    # Constraint: Only one active pricing per region/plan combination
    __table_args__ = (
        UniqueConstraint("region_id", "plan_id", "effective_from", name="uq_regional_pricing"),
    )


class SeasonalPricingSchedule(SoftDeleteMixin, Base):
    """
    Time-based pricing schedules for seasonal pricing.

    Allows setting different prices for different time periods.
    Use cases:
    - Holiday promotions (Black Friday, New Year)
    - Peak season pricing (Q4 for retail)
    - Off-season discounts (summer discounts)
    - Time-limited price increases

    Schedules can be:
    - One-time (specific date range)
    - Recurring (every year, every month, etc.)
    - Day-of-week based (weekday vs weekend pricing)
    """
    __tablename__ = "seasonal_pricing_schedules"

    id = Column(Integer, primary_key=True, index=True)

    # Schedule identification
    name = Column(String(200), nullable=False)  # "Black Friday 2024", "Summer Discount 2024"
    description = Column(Text, nullable=True)  # Detailed description of the pricing schedule

    # Pricing configuration
    postpaid_price_cents = Column(Integer, nullable=True)  # Override for postpaid pricing
    overage_price_cents = Column(Integer, nullable=True)  # Override for overage pricing

    # Plan-specific overrides (optional)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=True, index=True)  # Null = applies to all plans

    # Region-specific schedules (optional)
    region_id = Column(Integer, ForeignKey("geographic_regions.id"), nullable=True, index=True)  # Null = applies globally

    # Schedule timing
    start_date = Column(DateTime(timezone=True), nullable=False, index=True)
    end_date = Column(DateTime(timezone=True), nullable=False, index=True)

    # Recurrence configuration (for recurring schedules)
    is_recurring = Column(Boolean, default=False, nullable=False)
    recurrence_pattern = Column(JSONB, nullable=True)  # {"type": "yearly", "month": 11, "day": 25} for annual Christmas sale

    # Priority (for overlapping schedules)
    priority = Column(Integer, default=0, nullable=False)  # Higher priority wins when schedules overlap

    # Status
    is_active = Column(Boolean, default=True, nullable=False)

    # Price adjustment metadata
    discount_percentage = Column(Integer, nullable=True)  # Percentage discount (basis points)
    price_adjustment_reason = Column(Text, nullable=True)  # "Holiday promotion", "Peak season pricing", etc.

    # Metadata
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    approved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # Admin approval for price changes
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    plan = relationship("Plan")
    region = relationship("GeographicRegion")
    created_by = relationship("User", foreign_keys=[created_by_user_id])
    approved_by = relationship("User", foreign_keys=[approved_by_user_id])


# =============================================================================
# --- SECTION 13: PLATFORM LICENSE MANAGEMENT
# =============================================================================

class PlatformLicenseConfig(Base):
    """
    Stores the current platform license key for runtime updates.

    This is a singleton table (only one row) that stores the active license key.
    Allows updating the license without restarting the application.

    The license key is validated on:
    - Application startup (from .env or this table)
    - Every API request (via middleware)
    - Manual revalidation via admin UI

    Priority order for license key:
    1. Database (this table) - allows runtime updates
    2. Environment variable (PLATFORM_LICENSE_KEY) - initial/fallback
    """
    __tablename__ = "platform_license_config"

    id = Column(Integer, primary_key=True, default=1)  # Always ID=1 (singleton)
    license_key = Column(Text, nullable=False)  # The actual license key

    # Cached validation result (for performance)
    is_valid = Column(Boolean, default=False, nullable=False)
    validation_message = Column(Text, nullable=True)
    max_workers = Column(Integer, default=5, nullable=False)

    # License metadata (from decoded key)
    license_type = Column(String(50), nullable=True)  # "trial", "standard", "enterprise"
    customer_id = Column(String(255), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    hard_cutoff_at = Column(DateTime(timezone=True), nullable=True)

    # Grace period tracking
    in_grace_period = Column(Boolean, default=False, nullable=False)
    days_remaining = Column(Integer, nullable=True)
    days_until_cutoff = Column(Integer, nullable=True)

    # Update tracking
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Last validation timestamp
    last_validated_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    updated_by = relationship("User")

    # Ensure only one row exists
    __table_args__ = (
        # Check constraint to ensure only ID=1 exists
        # This is enforced at the application level too
        {},
    )


class PlatformGPUSettings(Base):
    """Platform-wide hardware tag configuration (singleton - ID=1).

    Admins enable which hardware tags are available for the deployment.
    Tags are open-ended strings — not limited to CUDA/ROCm/CPU.
    """
    __tablename__ = "platform_gpu_settings"

    id = Column(Integer, primary_key=True, default=1)
    hardware_tags = Column("gpu_backends", JSONB, nullable=False, default=lambda: ["cuda", "rocm", "cpu"])
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    updated_by = relationship("User")


class LLMCallUsage(Base):
    """One row per LLM call: who, which tool, which model, how many tokens.

    Per-call rather than a counter because a chat turn and a wizard call differ
    by more than an order of magnitude — measured at 50 versus 15,000 input
    tokens — and a counter cannot tell them apart, which makes it useless for
    pricing.

    `succeeded` is recorded because input tokens are consumed whether or not
    generation completed. Counting only successes would understate load exactly
    when the platform is under strain.
    """
    __tablename__ = "llm_call_usage"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=True)
    tool = Column(String(64), nullable=False, index=True)
    model = Column(String(255), nullable=True)
    base_url = Column(String(512), nullable=True)
    # "programmatic" (API key, however carried) or "interactive" (browser).
    channel = Column(String(16), nullable=False, default="unknown",
                     server_default="unknown")
    prompt_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    completion_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    succeeded = Column(Boolean, nullable=False, default=True, server_default="true")
    duration_ms = Column(Integer, nullable=True)
    occurred_at = Column(DateTime(timezone=True), server_default=func.now(),
                         nullable=False, index=True)


class TenantLLMUsageDaily(Base):
    """Daily rollup per (tenant, day, tool, model, channel).

    Mirrors TenantEgressUsageDaily. The per-call table answers "what happened";
    this answers "what did the month look like" without scanning every call.
    """
    __tablename__ = "tenant_llm_usage_daily"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    day = Column(Date, nullable=False, index=True)
    tool = Column(String(64), nullable=False)
    model = Column(String(255), nullable=False, default="", server_default="")
    channel = Column(String(16), nullable=False, default="unknown",
                     server_default="unknown")
    calls = Column(BigInteger, nullable=False, default=0)
    failed_calls = Column(BigInteger, nullable=False, default=0)
    prompt_tokens = Column(BigInteger, nullable=False, default=0)
    completion_tokens = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "day", "tool", "model", "channel",
                         name="uq_tenant_llm_usage_daily"),
    )


class PlatformLLMRateLimits(Base):
    """Platform-wide LLM rate limits (singleton, ID=1).

    The scarce resource is the shared model server's slot budget, not money.
    All five tools talk to one server whose concurrent_users is finite per
    preset, so an unbounded tenant degrades other tenants' latency rather than
    merely consuming its own budget.

    Two caps because the failure modes differ: `concurrent_max` catches a burst
    of parallel calls that exhausts slots at once, `window_max_calls` catches a
    steady loop that exhausts the day. Either alone leaves the other open.
    """
    __tablename__ = "platform_llm_rate_limits"

    id = Column(Integer, primary_key=True, default=1)
    # Off by default. An upgrade must not start refusing calls on its own.
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    concurrent_max = Column(Integer, nullable=False, default=4, server_default="4")
    window_seconds = Column(Integer, nullable=False, default=3600, server_default="3600")
    window_max_calls = Column(Integer, nullable=False, default=240, server_default="240")
    # The assistant is free and stays free; this is its volume ceiling, kept
    # separate so it can be loosened without loosening billable tools.
    assistant_window_max_calls = Column(Integer, nullable=False, default=120,
                                        server_default="120")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


class TenantLLMRateLimits(Base):
    """Per-tenant overrides. NULL on any column means INHERIT the platform
    default — which is not the same as zero. A NOT NULL DEFAULT 0 here would
    pin every tenant to "no calls allowed" the moment limiting was enabled.
    """
    __tablename__ = "tenant_llm_rate_limits"

    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
                       primary_key=True)
    enabled = Column(Boolean, nullable=True)
    concurrent_max = Column(Integer, nullable=True)
    window_seconds = Column(Integer, nullable=True)
    window_max_calls = Column(Integer, nullable=True)
    assistant_window_max_calls = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


class LLMRateLimitRejection(Base):
    """One row per refused call. A limit set too low is otherwise invisible
    until users complain; this makes it a pattern in the data instead.
    """
    __tablename__ = "llm_rate_limit_rejections"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    tool = Column(String(64), nullable=False)
    # Which cap was hit: "concurrent" | "window" | "assistant_window".
    cap = Column(String(32), nullable=False)
    occurred_at = Column(DateTime(timezone=True), server_default=func.now(),
                         nullable=False, index=True)


class PlatformLLMIdleSettings(Base):
    """Platform-wide idle-reclaim policy for managed-LLM stacks (singleton, ID=1).

    Previously these lived only in environment variables, so changing them meant
    a redeploy and a platform admin could neither see nor set them. Per-stack
    overrides live on PersistentStack; this row is the default they inherit.

    Resolution order is stack override -> this row -> env var, so a deployment
    that never touches this table behaves exactly as it did before.
    """
    __tablename__ = "platform_llm_idle_settings"

    id = Column(Integer, primary_key=True, default=1)
    # Off by default. Scale-to-zero is opted into, never inherited — an upgrade
    # must not silently start stopping a deployment's stacks.
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    idle_after_seconds = Column(Integer, nullable=False, default=3600, server_default="3600")
    # Tools whose backing capacity must never be reclaimed regardless of idleness.
    warm_required_tools = Column(JSONB, nullable=False, default=list, server_default="[]")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    updated_by = relationship("User")


class PlatformNoahSettings(Base):
    """Singleton (id=1) platform default for NOAH opportunistic idle-harvest.

    Per-worker overrides live on WorkerLicense (NULL = inherit this row). Off by
    default: a deployment that never touches this table harvests nothing, so an
    upgrade never silently starts running work on people's machines.
    """
    __tablename__ = "platform_noah_settings"

    id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    sustained_quiet_s = Column(Integer, nullable=False, default=300, server_default="300")
    pause_grace_s = Column(Integer, nullable=False, default=120, server_default="120")
    plugged_in_only = Column(Boolean, nullable=False, default=True, server_default="true")
    schedule_start = Column(String(5), nullable=True)  # "HH:MM" local; NULL = always
    schedule_end = Column(String(5), nullable=True)
    batch_appetite = Column(Integer, nullable=False, default=0, server_default="0")
    # Short fallback-release window (s) for an opportunistic worker that goes
    # heartbeat-stale, before the default offline threshold.
    stale_release_s = Column(Integer, nullable=False, default=75, server_default="75")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    updated_by = relationship("User")


class LaunchPromoCounter(Base):
    """launch-pricing-lockdown §6 — singleton (id=1) tracking the
    50%-off-first-3-months launch promo. Server-side cap (Stripe also
    enforces max_redemptions on the Coupon, so this is belt-and-suspenders).
    Counter persists across API restarts; never reset.

    Hold the coupon_id here so the FE banner countdown reads from one
    place and hot-swapping the promo is just an UPDATE.
    """
    __tablename__ = "launch_promo_counter"

    id = Column(Integer, primary_key=True, default=1)
    coupon_id = Column(String(255), nullable=False)
    redemptions_count = Column(Integer, nullable=False, default=0)
    max_redemptions = Column(Integer, nullable=False, default=100)
    updated_at = Column(DateTime(timezone=True),
                         server_default=func.now(), onupdate=func.now(),
                         nullable=False)


class LaunchPromoRedemption(Base):
    """One row per checkout session that successfully applied the
    launch promo. Provides:
      - Idempotency: keyed on stripe_checkout_session_id so a Stripe
        webhook retry doesn't double-increment the counter.
      - Audit trail: which tenant got which coupon when, for support.
    """
    __tablename__ = "launch_promo_redemptions"

    id = Column(Integer, primary_key=True)
    stripe_checkout_session_id = Column(String(255), unique=True, nullable=False, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    coupon_id = Column(String(255), nullable=False)
    redeemed_at = Column(DateTime(timezone=True),
                          server_default=func.now(), nullable=False)


class TenantAddonSubscription(Base):
    """launch-pricing-lockdown §5 — tracks each tenant's active
    add-on Stripe subscriptions so we can compute correct deltas on
    quantity changes and decrement on cancel.

    Each row = one Stripe subscription for one add-on SKU. A tenant
    can have multiple rows (e.g. 1 Credit Pack + 1 Storage Block,
    or 2 Credit Packs purchased separately). Quantity captures
    Stripe's `items[].quantity` field; when it changes via the
    portal we compute (new - old) × per_unit and adjust the
    tenant.bonus_* counter accordingly.
    """
    __tablename__ = "tenant_addon_subscriptions"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    stripe_subscription_id = Column(String(255), unique=True, nullable=False, index=True)
    # 'credit_pack' or 'storage_block' — derived from price_id at
    # webhook-handler time by matching against PlatformAddonSkus.
    addon_kind = Column(String(32), nullable=False, index=True)
    quantity = Column(Integer, nullable=False, default=1)
    stripe_price_id = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True),
                         server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True),
                         server_default=func.now(), onupdate=func.now(),
                         nullable=False)


class SubscriptionCancellation(Base):
    """launch-pricing-lockdown — captures churn reason data from
    Stripe `customer.subscription.updated/deleted` events when the
    customer cancels via the Stripe portal.

    Stripe sends `cancellation_details` in the event payload when the
    "Collect cancellation reason" toggle is ON in the Customer Portal
    settings. Three fields land here:
      reason   — canned bucket (e.g. "too_expensive")
      feedback — slightly more granular selection
      comment  — optional free-text the customer typed

    All three may be NULL if the toggle is off in Stripe or the
    customer skipped the prompt.

    One row per cancellation event. If the same subscription is
    cancelled and re-activated and re-cancelled, multiple rows.
    """
    __tablename__ = "subscription_cancellations"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    stripe_subscription_id = Column(String(255), nullable=False, index=True)
    cancelled_at = Column(DateTime(timezone=True),
                          server_default=func.now(), nullable=False, index=True)
    reason = Column(String(64), nullable=True, index=True)
    feedback = Column(String(64), nullable=True)
    comment = Column(Text, nullable=True)
    # Snapshot of the plan at cancellation time so churn analysis
    # doesn't require joining to a possibly-renamed plan row later.
    plan_name_at_cancel = Column(String(100), nullable=True)


class PlatformAddonSkus(Base):
    """launch-pricing-lockdown §5 — singleton (id=1) mapping the
    add-on Stripe Prices to their effect on tenant.bonus_* counters.

    The Stripe webhook handler resolves an incoming subscription
    event's price_id against this row to know whether to bump
    bonus_credits (Credit Pack) or bonus_storage_gb (Storage Block).
    Per-unit values let us tune the bonus without code changes.

    Per-tier add-on entitlement (which paid plans can buy add-ons)
    is enforced separately in the buy endpoint, not here.
    """
    __tablename__ = "platform_addon_skus"

    id = Column(Integer, primary_key=True, default=1)
    credit_pack_price_id = Column(String(255), nullable=True)
    credit_pack_credits_per_unit = Column(BigInteger, nullable=False, default=1000)
    storage_block_price_id = Column(String(255), nullable=True)
    storage_block_gb_per_unit = Column(Integer, nullable=False, default=100)
    seat_block_price_id = Column(String(255), nullable=True)
    seat_block_seats_per_unit = Column(Integer, nullable=False, default=1, server_default='1')
    egress_block_price_id = Column(String(255), nullable=True)
    egress_block_gb_per_unit = Column(Integer, nullable=False, default=100, server_default='100')
    updated_at = Column(DateTime(timezone=True),
                         server_default=func.now(), onupdate=func.now(),
                         nullable=False)


class PlatformPoolAlertState(Base):
    """launch-pricing-lockdown §8.4 — singleton (id=1) tracking GPU
    pool queue-wait alert state. Persists across API restarts so an
    in-progress breach window doesn't reset on every restart.

    A "breach" = avg wait > threshold (default 30s) in the most
    recent 5-min window. If `first_observed_at` is set and the
    breach has been continuous for `sustained_minutes` (default 5)
    AND the last alert was sent more than `cooldown_minutes` ago
    (default 60) — fire an email.
    """
    __tablename__ = "platform_pool_alert_state"

    id = Column(Integer, primary_key=True, default=1)
    # NULL means "no current breach observed."
    first_observed_at = Column(DateTime(timezone=True), nullable=True)
    # NULL means "alert never sent."
    last_alerted_at = Column(DateTime(timezone=True), nullable=True)
    # Snapshot of the avg-wait-seconds at the most recent check, for
    # debugging and the dashboard "current alert state" surface.
    last_avg_wait_seconds = Column(Float, nullable=True)
    updated_at = Column(DateTime(timezone=True),
                         server_default=func.now(), onupdate=func.now(),
                         nullable=False)


class PlatformFirstRunStatus(Base):
    """LDF-4 §11 — Platform first-run completion gate (singleton ID=1).

    On a fresh deploy `completed` is false; the Dashboard middleware
    redirects every non-auth route to /first-run.html until an
    operator submits the platform admin's intended email + username
    via POST /api/platform/first-run. The form rejects the literal
    shipping defaults (the bootstrap admin email, admin) AND every value
    rejected by the LDF-3 §10 validators (denylist usernames,
    homoglyph-laden email locals, etc.).

    The persisted email/username are advisory — the actual platform
    admin is still elevated by the first-signup bootstrap in
    routers/onboarding.py. This flag's purpose is to gate the
    Dashboard so the OPERATOR knows they need to configure their
    admin identity before the first signup happens (so a random
    visitor doesn't accidentally get auto-elevated).
    """
    __tablename__ = "platform_first_run_status"

    id = Column(Integer, primary_key=True, default=1)
    completed = Column(Boolean, nullable=False, default=False, server_default="false")
    completed_at = Column(DateTime(timezone=True), nullable=True)
    admin_email = Column(String(254), nullable=True)
    admin_username = Column(String(64), nullable=True)


class NIMCatalogEntry(Base):
    """Known NVIDIA NIM models and their API configurations.

    Synced from nim_catalog.json on startup. Used by the workflow builder
    to populate the NIM model dropdown and auto-configure node I/O types.
    """
    __tablename__ = "nim_catalog"
    id = Column(String(100), primary_key=True)  # e.g. "grounding-dino"
    name = Column(String(200), nullable=False)
    category = Column(String(50), nullable=False, index=True)
    description = Column(Text, nullable=True)
    endpoint_path = Column(String(500), nullable=False)
    input_type = Column(String(50), nullable=False)
    output_type = Column(String(50), nullable=False)
    input_mode = Column(String(50), nullable=False)
    output_mode = Column(String(50), nullable=False)
    request_format = Column(String(50), nullable=False)
    async_polling = Column(Boolean, default=False, nullable=False)
    gpu_required = Column(Boolean, default=True, nullable=False)
    min_vram_gb = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class OnboardingChecklistReward(Base):
    """Tracks whether a tenant has claimed their onboarding completion reward."""
    __tablename__ = "onboarding_checklist_rewards"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), unique=True, nullable=False, index=True)
    claimed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    credits_granted = Column(BigInteger, default=500, nullable=False)

    tenant = relationship("Tenant")


class PendingPermissionEmail(Base):
    """Queue row for the daily permission-update digest.

    Per-event sends (workflow-complete, permission-expiration) used to
    fire immediately and pile up dozens of "MOD permission update"
    subject-line emails per day per operator. They now INSERT here and
    a 15-min APScheduler job drains rows once per user per day in the
    09:00 user-local window, renders one HTML digest, sends it, stamps
    `sent_at` on every drained row.

    Time-sensitive emails — billing (invoice / payment_retry /
    grace_period), permission_request (someone needs to approve),
    invitations, welcome — bypass this queue and still send
    immediately. See utils.permission_digest.queue_digest_event for
    the inserter and services.permission_digest for the drainer.
    """
    __tablename__ = "pending_permission_emails"

    id = Column(BigInteger, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    # Wrap the default in sqlalchemy.text() so the literal '{}'::jsonb
    # gets emitted as raw SQL — passing the string directly causes
    # SQLAlchemy to double-quote it ("'''{}''::jsonb'") and Postgres
    # rejects the table-create with "invalid input syntax for type json".
    event_payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id])


class UsageAlertTracker(Base):
    """Tracks which usage alerts have been sent to avoid duplicates within a billing period."""
    __tablename__ = "usage_alert_tracker"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    alert_type = Column(String(50), nullable=False)
    period_key = Column(String(10), nullable=False)
    sent_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "alert_type", "period_key", name="uq_usage_alert_tenant_type_period"),
    )


class UserSession(Base):
    __tablename__ = "user_sessions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    keycloak_session_id = Column(String(255), nullable=True, index=True)
    device_info = Column(String(500), nullable=True)
    ip_address = Column(String(45), nullable=True)
    last_active_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", foreign_keys=[user_id])


class ChangelogEntry(SoftDeleteMixin, Base):
    """A "What's New" release note (item #5 / capability
    `whats-new-publishing`). Drives both the in-product changelog
    feed and the platform-admin authoring form. Scheduled-publish
    flips `is_published` to True automatically once the wall clock
    crosses `scheduled_for`."""
    __tablename__ = "changelog_entries"
    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    version = Column(String(50), nullable=True)
    is_published = Column(Boolean, default=False, server_default='f', nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # What's-New extension fields (#5). Added via _run_migration so
    # existing rows pick them up on next startup; create_all handles
    # fresh installs.
    summary = Column(String(500), nullable=True)
    hero_image_url = Column(String, nullable=True)
    tags = Column(JSONB, nullable=True)
    slug = Column(String(160), nullable=True, unique=True, index=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=True, index=True)
    # 'user_facing' (visible in the tenant What's-New widget) or
    # 'internal' (only the platform-admin's draft view sees it).
    # Defaults to 'internal' so a forgotten flag never leaks an
    # internal release note to tenants. Required choice on publish —
    # the admin endpoint enforces an explicit audience at publish time.
    audience = Column(String(20), nullable=False, server_default='internal', default='internal')


# ---------------------------------------------------------------------------
# Tenant Harbor — quota alerts + daily usage snapshots (Phase 11)
# ---------------------------------------------------------------------------

class HarborQuotaAlert(Base):
    """One row per (tenant_id, threshold) alert we've already dispatched
    inside a 24h window. Keeps check_harbor_quotas idempotent — without
    it a cold-started scheduler would re-email the same tenant every
    hour until they delete images."""
    __tablename__ = "harbor_quota_alerts"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    # Threshold that tripped: 80 or 95 (percentage of hard quota).
    threshold = Column(Integer, nullable=False)
    sent_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Whether an email was actually dispatched (for audit; a threshold
    # crossing without an email — e.g. tenant has no billing_email —
    # still records a row so we don't retry every hour).
    email_sent = Column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "threshold", "sent_at",
                         name="uq_harbor_quota_alert_tenant_threshold_sent"),
    )


class TenantHarborUsageDaily(Base):
    """Daily snapshot of each tenant's Harbor storage. Written by
    snapshot_harbor_usage scheduler (daily). Monthly billing
    reconciles against these rows to bill storage overage."""
    __tablename__ = "tenant_harbor_usage_daily"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    captured_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    used_bytes = Column(BigInteger, nullable=False, default=0)
    artifact_count = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        # Prevent duplicate rows if the scheduler fires twice on the same day.
        UniqueConstraint("tenant_id", "captured_at",
                         name="uq_tenant_harbor_usage_daily_tenant_captured"),
    )


class TenantEgressUsageDaily(Base):
    """Daily egress (bytes sent OUT of MinIO) per tenant, written by the
    egress-metering scheduler from MinIO's per-bucket traffic_sent_bytes
    (tenant-{id}-files). One row per (tenant, day); the job upserts
    egress_bytes += interval-delta. Monthly billing reconciles against these
    rows to bill egress overage beyond the plan's included_egress_gb."""
    __tablename__ = "tenant_egress_usage_daily"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    day = Column(Date, nullable=False, index=True)
    egress_bytes = Column(BigInteger, nullable=False, default=0)  # accumulated bytes-out for this day
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "day",
                         name="uq_tenant_egress_usage_daily_tenant_day"),
    )


class MinioBucketEgressState(Base):
    """Last-seen cumulative MinIO traffic_sent_bytes per bucket, so the
    egress-metering job can compute per-interval deltas. The MinIO metric is a
    gauge cumulative since server start, so a drop (current < last) means MinIO
    restarted and that interval's delta is taken as `current` (not negative).
    Keyed by bucket name (one MinIO per cell)."""
    __tablename__ = "minio_bucket_egress_state"
    bucket = Column(String(255), primary_key=True)
    last_sent_bytes = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TenantRelayUsageDaily(Base):
    """Daily DERP-relay bytes per tenant (tailnet-plan Group 8), written by the
    relay-metering scheduler from a MOD-operated DERP's per-account byte counters.
    One row per (tenant, day); the job upserts relay_bytes += interval-delta.
    Monthly billing reconciles against these rows to bill relay overage beyond the
    plan's included_relay_gb — a distinct line from egress, a token draw from the
    same balance. Mirror of TenantEgressUsageDaily."""
    __tablename__ = "tenant_relay_usage_daily"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    day = Column(Date, nullable=False, index=True)
    relay_bytes = Column(BigInteger, nullable=False, default=0)  # accumulated MOD-relayed bytes for this day
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "day",
                         name="uq_tenant_relay_usage_daily_tenant_day"),
    )


class DerpRelayEgressState(Base):
    """Last-seen cumulative MOD-DERP relay bytes per account, so the relay-metering
    job can compute per-interval deltas. The DERP counter is cumulative since the
    relay started, so a drop (current < last) means the DERP restarted and that
    interval's delta is taken as `current` (not negative). Keyed by the account key
    the DERP verify callback attributed the node to (the tenant id as string, or a
    node-scoped key). Mirror of MinioBucketEgressState."""
    __tablename__ = "derp_relay_egress_state"
    account_key = Column(String(255), primary_key=True)
    last_sent_bytes = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ControlServerConfig(Base):
    """tailnet-plan Groups 1 + 7 — singleton (id=1) install-level posture for the
    mesh control server and DERP relay. `control_server_tier` (disabled /
    mod-hosted / customer-owned) says WHERE the coordinator lives; `relay_owner`
    (mod-derp / customer-derp / direct-only) says WHO relays NAT-blocked traffic;
    `coordinator_location` is the external coordinator URL for customer-owned.

    Every value is NULLABLE and NULL means "derive from today's env reality"
    (services/control_server_tier.resolve_*), so the row is additive and inert —
    an install with no row behaves exactly as it does today. Same shape as
    MeshReplicationConfig / SchedulerRuntimeConfig (DB → env → default, source
    surfaced)."""
    __tablename__ = "control_server_config"

    singleton_id = Column(Integer, primary_key=True, default=1)
    control_server_tier = Column(String(32), nullable=True)   # disabled|mod-hosted|customer-owned
    relay_owner = Column(String(32), nullable=True)           # mod-derp|customer-derp|direct-only
    coordinator_location = Column(String(512), nullable=True)  # external coordinator URL (customer-owned)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    audit_event_id = Column(BigInteger, ForeignKey("audit_events.id"), nullable=True)

    updated_by = relationship("User", foreign_keys=[updated_by_user_id])

    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_control_server_config_singleton"),
    )


class RelayAuthorizedNode(Base):
    """tailnet-plan task 7.9 — maps a tailnet node public key → the billing tenant
    for MOD DERP relay authorization + attribution. The verify-client-URL callback
    resolves a connecting node to its account through this table. `source` records
    how the mapping was created: `mod-hosted` (the node is a MOD-coordinated node)
    or `relay-token` (a pasted relay token bound a customer-owned install's nodes).
    A revoked (revoked_at set) row denies relay."""
    __tablename__ = "relay_authorized_node"
    node_key = Column(String(255), primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    source = Column(String(32), nullable=False, default="mod-hosted")  # mod-hosted | relay-token
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class PortOutcome(Base):
    """P9a — learning table: one row per (repo, variant) port attempt.

    Detector consults this before running preflight; if 3+ consistent
    rows exist for (source_hash, variant) we skip the preflight and
    serve the cached verdict. This lets the wizard be FAST on repeat
    attempts of the same repo (common: user iterates on a workflow built
    around the same `hustvl/4DGaussians`) and gets smarter over time
    as users collectively port more repos.

    `source_hash` is tenant-agnostic (SHA-256 over the canonical
    filename:body manifest) so two tenants uploading the same repo
    share learnings. `repo_url` is a secondary key for when the user
    provided a git URL — some repos vary slightly between tenants'
    checkouts but share the same URL.

    `porter_version` namespaces outcomes by the porter's revision
    (kernel_porter.PORTER_VERSION) so a hipify upgrade / dpct →
    SYCLomatic migration invalidates cached verdicts automatically.
    """
    __tablename__ = "port_outcomes"
    id = Column(Integer, primary_key=True)
    source_hash = Column(String(64), index=True, nullable=False)
    repo_url = Column(String, index=True, nullable=True)
    variant = Column(String(32), index=True, nullable=False)
    porter_version = Column(String(16), nullable=False, default="v1")
    compatibility = Column(String(32), nullable=False)  # compatible | not_recommended | incompatible
    reason = Column(Text, nullable=True)
    # Per-translator diagnostic payload — hipify output, dpct warnings,
    # ONNX export log. Useful for the Port Coverage dashboard's top-
    # failing-imports drill-in (Phase 9g).
    diagnostics = Column(JSONB, nullable=True)
    # Phase 9j — shadow-run window records disagreements between
    # dpct and SYCLomatic on the same input. NULL when only one
    # translator ran.
    translator_disagreement = Column(JSONB, nullable=True)
    # Phase 9l — ref_cuda_ratio (throughput vs CUDA baseline). Populated
    # by the smoke-verification benchmark; NULL when not yet run.
    ref_cuda_ratio = Column(Float, nullable=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        # Common query: "what's the cached verdict for this repo on this
        # variant under this porter version?" — covered index speeds it up.
        Index(
            "idx_port_outcomes_hash_variant_ver",
            "source_hash", "variant", "porter_version",
        ),
    )


class TenantImageDeletion(Base):
    """ISO-27001-compliant soft-delete record for a tenant Harbor image
    (item #8 / capability `tenant-registry-admin`).

    Deletion intent + an `retention_until` timestamp are recorded here.
    The image is hidden from the tenant's listing immediately, but the
    actual blob is retained in Harbor until a background reaper runs
    after `retention_until`. No UI path triggers a hard delete — only
    the reaper does, after the retention window.
    """

    __tablename__ = "tenant_image_deletions"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repository = Column(String, nullable=False, index=True)
    tag = Column(String, nullable=False)
    digest = Column(String, nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    deleted_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    deleted_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    retention_until = Column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    # Set when the reaper has hard-deleted the artifact from Harbor.
    # Rows with reaped_at IS NULL are still in the retention window
    # (or the reaper hasn't gotten to them yet); rows with
    # reaped_at IS NOT NULL are post-reaping audit history.
    reaped_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        Index(
            "idx_tenant_image_deletions_tenant_repo_tag",
            "tenant_id", "repository", "tag",
        ),
    )


# =============================================================================
# --- Tier 4 follow-up: persistent build-remediation memory
# =============================================================================
# Each successful LLM build-failure remediation gets recorded here. Future
# builds whose normalised error signature matches an entry try the cached
# diff FIRST (deterministic, ~5 ms instead of ~30 s) and only call the LLM
# when no cached fix exists or the cached fix fails to apply.
#
# Hash-based exact-match on signature is intentionally simple — embedding
# search / semantic similarity is a future upgrade. The signature is
# computed by the imagebuilder before recording; see
# CORE/DockerContainers/ImageBuilder/llm_remediation.py::error_signature.

class BuildRemediation(Base):
    __tablename__ = "build_remediations"
    id = Column(Integer, primary_key=True)
    # SHA-256 of the normalised error tail (truncated to 64 hex chars so
    # equality joins stay fast). Multiple entries CAN share a signature
    # if different diffs were tried for the same error class — lookups
    # order by success_count DESC so the most-validated patch wins.
    error_signature = Column(String(64), nullable=False, index=True)
    # First few hundred chars of the actual (un-normalised) error tail.
    # Stored for human triage / debugging; never used for matching.
    error_sample = Column(Text)
    # The unified-diff that fixed this error. Repeats are deduped via
    # SHA-256 of the diff body — see diff_hash.
    diff = Column(Text, nullable=False)
    diff_hash = Column(String(64), nullable=False, index=True)
    # Which LLM produced the diff originally; mostly for audit so we can
    # tell if model changes correlate with fix quality.
    model = Column(String(100))
    # How many times this exact (signature, diff) pair has been
    # successfully replayed on a subsequent build. Starts at 1 for the
    # original recording; each cache hit on a successful retry +1.
    success_count = Column(Integer, default=1, nullable=False)
    # Tier-B trust scoring: increment when a cached diff is applied
    # but the build retry STILL fails, OR when the cached diff fails
    # to apply at all against the current Dockerfile. Lookups order
    # by (success - failure) so a fix that's flipped to harmful
    # doesn't keep getting recommended.
    failure_count = Column(Integer, default=0, nullable=False)
    # When the last build hit this cached fix and retry-fail'd. NULL
    # for entries that have never failed. Used by future time-decay
    # ranking — recent failures should weigh more than ancient ones.
    last_failure_at = Column(DateTime(timezone=True), nullable=True)
    # Auto-promotion timestamp. Set when ``success_count`` clears
    # PROMOTION_THRESHOLD (default 10) AND ``failure_count <= 1``.
    # Promoted entries are exposed via /remediation/promoted for the
    # imagebuilder to fast-path (skip the LLM loop entirely on a
    # promoted match) and for tooling that wants to copy them into
    # the static known_failures.py catalog.
    promoted_at = Column(DateTime(timezone=True), nullable=True)
    # Most recent build that used (or recorded) this entry. Powers the
    # "least recently used" reaper if/when this table needs trimming.
    last_used_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # Lookup index: (signature) → entries ordered by success_count.
        # Imagebuilder picks the highest-success_count diff per signature.
        Index(
            "ix_build_remediations_signature_success",
            "error_signature", "success_count",
        ),
        # Upsert key: (signature, diff_hash) is unique. The same diff
        # for the same error signature should never have two rows;
        # we increment success_count instead.
        UniqueConstraint(
            "error_signature", "diff_hash",
            name="uq_build_remediation_sig_diff",
        ),
    )


# =============================================================================
# --- Tier E: federation — remote remediation entries pulled in from peers
# =============================================================================
# Mirror of BuildRemediation but populated from federated peers' shares,
# never from a local build. Kept in a separate table so the local trust
# accounting (success_count, promotion eligibility, etc.) doesn't get
# diluted by foreign signals — federation hits are looked up AFTER the
# local table misses, with their own trust column copied from the
# source peer.
#
# diff body is nullable here: peers running ``federation_mode=stats``
# share the signature + diff_hash + counters but not the body. The
# imagebuilder treats stats-only hits as "this is a known issue
# elsewhere" signal (still calls the LLM, but with the federation
# signature/hash threaded into the prompt as a hint).

class FederatedRemediation(Base):
    __tablename__ = "federated_remediations"
    id = Column(Integer, primary_key=True)
    # Same signature as the local build_remediations table — match
    # against this in lookup.
    error_signature = Column(String(64), nullable=False, index=True)
    # The peer's diff_hash. Two peers with the same fix for the same
    # signature share this hash; the imagebuilder can use that for
    # cross-deployment dedupe in the trust signal.
    diff_hash = Column(String(64), nullable=False, index=True)
    # Diff body — populated only when the source peer shares in
    # ``full`` mode. NULL on stats-only shares.
    diff = Column(Text, nullable=True)
    # Origin peer URL (e.g. https://example.com).
    # Used for audit + per-peer trust weighting if ever added.
    source_peer = Column(String(255), nullable=False)
    # Counts as reported by the source peer at last share. Local
    # success_count on a federated diff is NOT mutated when used —
    # the local entry, if any, tracks local replays.
    success_count = Column(Integer, default=1, nullable=False)
    failure_count = Column(Integer, default=0, nullable=False)
    # Did the source peer's local row clear promotion thresholds?
    # ``True`` = high-trust at the origin. Imagebuilder weights
    # promoted-elsewhere fixes higher than non-promoted.
    promoted_at_source = Column(Boolean, default=False, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # Lookup by signature; many peers may have the same fix.
        Index(
            "ix_federated_remediations_signature",
            "error_signature",
        ),
        # Upsert key: same fix from same peer dedupes — counter
        # values get refreshed instead of duplicated.
        UniqueConstraint(
            "error_signature", "diff_hash", "source_peer",
            name="uq_federated_remediation_sig_hash_peer",
        ),
    )


# =============================================================================
# --- Foolproof #3: source-hash fast path for unchanged rebuilds
# =============================================================================
# When a node's (Dockerfile + entrypoint + source files) hashes to the same
# bytes as a prior successful build, there's no reason to spawn `docker
# build` again — just retag + push the existing image. Saves 5-30 minutes
# of compile + dep-pull wall-time on every "rebuild without changes" cycle
# (which is most rebuilds for an established node).

class BuiltImageCache(Base):
    __tablename__ = "built_image_cache"
    id = Column(Integer, primary_key=True)
    # SHA-256 of the full build context (sorted file list + bodies),
    # 64 hex chars. The imagebuilder computes this BEFORE `docker
    # build`; the API hands back a cache hit when the same hash has
    # been built successfully before.
    source_hash = Column(String(64), nullable=False, index=True)
    # Image name without registry prefix (e.g. "trellis2-image-to-3d").
    # Indexed alongside source_hash because the same source hash COULD
    # legitimately exist for different node names (template repo
    # forked into two nodes); we want each node to track its own
    # builds.
    image_name = Column(String(255), nullable=False, index=True)
    # Variant key — "hero", "cuda", "rocm", etc. Each variant of the
    # same source has its own build because the FROM line differs.
    variant = Column(String(32), nullable=False, default="hero")
    # The successfully-built image's content digest. ``docker pull
    # <registry>/<name>@sha256:<digest>`` always resolves to the
    # same bytes regardless of which tag points at it — the digest
    # is what makes retag+push safe.
    image_digest = Column(String(80), nullable=False)
    # Registry the image lives in (e.g. "example.com").
    # Captured so the imagebuilder knows where to docker-pull from
    # before retagging into the tenant harbor.
    source_registry = Column(String(255), nullable=False)
    # How many subsequent rebuilds reused this entry — operator
    # signal of how much wall-time the cache is saving.
    hit_count = Column(Integer, default=0, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_built_image_cache_hash_name_variant",
            "source_hash", "image_name", "variant",
        ),
        UniqueConstraint(
            "source_hash", "image_name", "variant",
            name="uq_built_image_cache_hash_name_variant",
        ),
    )


class WizardLearnedCrossover(Base):
    """human-assisted-discovery — a crossover entry LEARNED from a human's
    answer to a describe-search clarifying question, rather than hand-curated
    in node_builder/jargon_map.py.

    When the describe-search verdict is WEAK (no curated crossover, no learned
    crossover, and the scored candidates are worth-showing-thin) the wizard asks
    the user to name a library / GitHub repo / technical term. The parsed answer
    is folded into the search AND recorded here, keyed to the description's
    arctic-embed embedding, so a future near-identical description resolves
    without asking again.

    Retrieval is cosine-in-Python (NO pgvector): the query description is
    embedded and compared against this tenant's rows plus any row an operator
    has promoted with ``approved_cross_tenant``. See
    node_builder/learned_crossover.py.
    """
    __tablename__ = "wizard_learned_crossovers"

    id = Column(Integer, primary_key=True)
    # Owning tenant — a learned entry is private to its tenant unless an
    # operator promotes it cross-tenant.
    tenant_id = Column(Integer, nullable=False, index=True)
    # The user description that triggered the clarification. Kept verbatim for
    # the operator "misses / learned" review surfaces.
    description = Column(Text, nullable=False)
    # arctic-embed unit-normalised embedding of `description` (list[float],
    # 1024-dim by default). JSONB, not pgvector — cosine is computed in Python.
    embedding = Column(JSONB, nullable=False, default=list)
    # The crossover payload parsed from the human answer, mirroring the curated
    # crossover_for() shape: jargon terms, github "owner/repo" refs, pypi names.
    jargon = Column(JSONB, nullable=False, default=list)
    github = Column(JSONB, nullable=False, default=list)
    pypi = Column(JSONB, nullable=False, default=list)
    # Operator gate: false = private to `tenant_id`; true = inert (GB.LEAK.1:
    # cross-tenant promotion disabled). Column retained for contract stability,
    # no code reads this flag for matching logic.
    approved_cross_tenant = Column(Boolean, nullable=False, default=False)
    # User who supplied the answer (nullable — a system/backfill path may have
    # no user context).
    created_by = Column(Integer, nullable=True)
    # How many later queries matched this row — operator signal of usefulness.
    hit_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)

    __table_args__ = (
        Index("ix_wizard_learned_crossovers_tenant_approved",
              "tenant_id", "approved_cross_tenant"),
    )


class SchedulerRuntimeConfig(Base):
    """scheduler-runtime-config-panel — singleton (id=1) row holding the
    platform-admin-tunable scheduler aggressiveness knobs. Loaded into
    services.scheduler_runtime_cache at startup; queue_packer +
    calibration_dispatcher read from the cache on every relevant call.
    """
    __tablename__ = "scheduler_runtime_config"

    singleton_id = Column(Integer, primary_key=True, default=1)
    preset_name = Column(String(20), nullable=False, default="normal")
    capacity_floor_pct = Column(SmallInteger, nullable=False, default=30)
    packer_full_above_util_pct = Column(SmallInteger, nullable=False, default=70)
    variance_penalty_s = Column(SmallInteger, nullable=False, default=15)
    # Shared ONGOING cap on background work (calibration + variant-build
    # backfill): the max fraction of total fleet slots they may occupy at
    # once. capacity_floor_pct is the START gate ("don't begin unless X%
    # free"); this is the cap that keeps background from ever growing to
    # saturate the fleet — there's always room for real user jobs.
    background_max_fleet_pct = Column(SmallInteger, nullable=False, default=40)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    audit_event_id = Column(BigInteger, ForeignKey("audit_events.id"), nullable=True)

    updated_by = relationship("User", foreign_keys=[updated_by_user_id])

    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_scheduler_runtime_config_singleton"),
        CheckConstraint(
            "capacity_floor_pct BETWEEN 0 AND 90",
            name="ck_scheduler_runtime_config_capacity_floor_range",
        ),
        CheckConstraint(
            "packer_full_above_util_pct BETWEEN 10 AND 100",
            name="ck_scheduler_runtime_config_packer_full_above_range",
        ),
        CheckConstraint(
            "variance_penalty_s BETWEEN 1 AND 60",
            name="ck_scheduler_runtime_config_variance_penalty_range",
        ),
        CheckConstraint(
            "background_max_fleet_pct BETWEEN 5 AND 90",
            name="ck_scheduler_runtime_config_background_max_fleet_range",
        ),
        CheckConstraint(
            "preset_name IN ('aggressive', 'normal', 'light', 'custom')",
            name="ck_scheduler_runtime_config_preset_name",
        ),
    )


class MeshReplicationConfig(Base):
    """add-multi-region-mesh §4.2 — singleton (id=1) row holding the platform-
    admin-tunable cross-region replication RPO dial (the data-loss window if a
    region dies). `rpo_seconds` low = stream continuously (safe, more egress);
    high = batch every rpo_seconds (cheap, more risk). The replication layer
    reads `replication_cadence(rpo_seconds)` to choose streaming vs batch.
    """
    __tablename__ = "mesh_replication_config"

    singleton_id = Column(Integer, primary_key=True, default=1)
    rpo_seconds = Column(Integer, nullable=False, default=5)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    audit_event_id = Column(BigInteger, ForeignKey("audit_events.id"), nullable=True)

    updated_by = relationship("User", foreign_keys=[updated_by_user_id])

    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_mesh_replication_config_singleton"),
        CheckConstraint(
            "rpo_seconds BETWEEN 1 AND 86400",
            name="ck_mesh_replication_config_rpo_range",
        ),
    )


class StackRebalancerConfig(Base):
    """add-persistent-stacks §8.2 — singleton (id=1) row holding the admin-tunable
    persistent-stack rebalancer "only move if meaningfully better" threshold
    (fractional, e.g. 0.10 = move only when a peer worker is ≥10% freer).
    The hourly rebalancer reads this (DB → env STACK_REBALANCE_THRESHOLD → default).
    """
    __tablename__ = "stack_rebalancer_config"

    singleton_id = Column(Integer, primary_key=True, default=1)
    threshold = Column(Float, nullable=False, default=0.10)
    # Master on/off for automatic stack relocation. Defaults OFF: until the
    # lifecycle handler landed, no stack ever reached "running", so
    # rebalance_once() filtered every real stack out and this code path has
    # never executed against a live deployment. Fixing the lifecycle makes it
    # eligible for the first time — enabling it must be a deliberate act, not
    # a side effect of upgrading.
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_stack_rebalancer_config_singleton"),
        CheckConstraint("threshold >= 0 AND threshold <= 10",
                        name="ck_stack_rebalancer_config_threshold_range"),
    )


class StackReconcilerConfig(Base):
    """Singleton (id=1) row for the persistent-stack recovery reconciler's
    admin-tunable master enable + live-mode, so operators control recovery from
    the UI instead of only via env vars. Resolution is DB → env
    (STACK_RECONCILER_ENABLED / STACK_RECONCILER_LIVE) → default, mirroring
    StackRebalancerConfig.

    `enabled` — whether the reconciler job runs at all (dry-run still costs a
    query per tick). Defaults ON: reporting is safe.
    `live_mode` — whether it may actually enqueue recovery work. Defaults OFF:
    acting on a stack is a deliberate act, not a side effect of upgrading, so
    it stays dry-run until an operator opts in.
    """
    __tablename__ = "stack_reconciler_config"

    singleton_id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    live_mode = Column(Boolean, nullable=False, default=False, server_default="false")
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_stack_reconciler_config_singleton"),
    )


# =============================================================================
# --- SECTION 14: TENANT RESOURCE MONITOR
# =============================================================================

class TaskResourceAggregate(Base):
    """tenant-resource-monitor — one row per completed api_task_queue task.

    Derived from the task's TaskResourceSample stream by the hourly
    aggregator. Immutable: once written, neither updated nor re-rated.
    Each row freezes the cost vector against the rates at the moment of
    aggregation (rate_snapshot) so historical investigation does not
    depend on the live ResourceMonitorConfig row.
    """
    __tablename__ = "task_resource_aggregate"

    id = Column(BigInteger, primary_key=True, index=True)
    task_id = Column(
        Integer,
        ForeignKey("api_task_queue.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # Native resource totals — the leak detector compares these
    # directly against per-resource thresholds (no dollarization).
    gpu_seconds = Column(Float, nullable=False, default=0.0, server_default="0")
    cpu_core_seconds = Column(Float, nullable=False, default=0.0, server_default="0")
    ram_gb_seconds = Column(Float, nullable=False, default=0.0, server_default="0")
    vram_gb_seconds = Column(Float, nullable=False, default=0.0, server_default="0")
    disk_read_mb = Column(Float, nullable=False, default=0.0, server_default="0")
    disk_write_mb = Column(Float, nullable=False, default=0.0, server_default="0")
    wall_clock_seconds = Column(Float, nullable=False, default=0.0, server_default="0")

    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    aggregated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    task = relationship("ApiTaskQueue", foreign_keys=[task_id])
    tenant = relationship("Tenant", foreign_keys=[tenant_id])
    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_task_resource_aggregate_tenant_completed", "tenant_id", "completed_at"),
        Index("ix_task_resource_aggregate_user_completed", "user_id", "completed_at"),
        CheckConstraint(
            "gpu_seconds >= 0 AND cpu_core_seconds >= 0 AND ram_gb_seconds >= 0 "
            "AND vram_gb_seconds >= 0 AND disk_read_mb >= 0 AND disk_write_mb >= 0 "
            "AND wall_clock_seconds >= 0",
            name="ck_task_resource_aggregate_nonneg_totals",
        ),
    )


class ResourceMonitorConfig(Base):
    """tenant-resource-monitor — singleton (id=1) row holding the
    platform-admin-tunable per-resource leak-detection thresholds.

    Per-resource means each metric (GPU-seconds, CPU-core-seconds,
    RAM GB-seconds, IO MB) has its own threshold expressed as a
    native-unit-per-credit-billed ratio. The leak scan compares each
    resource's actual ratio against its threshold independently;
    audit events capture which resource(s) tripped so operators see
    *which* dimension is being abused rather than a single
    dollarized rollup.

    Same pattern as SchedulerRuntimeConfig: typed columns, range
    CheckConstraints, audit_event_id FK linking to the audit row that
    recorded the most-recent change.
    """
    __tablename__ = "resource_monitor_config"

    singleton_id = Column(Integer, primary_key=True, default=1)

    # Per-resource leak thresholds. Each is "native units of this
    # resource per 1 credit billed". When the actual ratio for a
    # (tenant, user) over a 24h window exceeds the threshold for any
    # resource, a leak event fires for that resource.
    #
    # Defaults assume 1 credit ≈ 10 GPU-seconds of compute at the
    # current pricing tier. Operators tune from there based on what
    # the data actually shows.
    gpu_seconds_per_credit_threshold = Column(
        Float, nullable=False, default=60.0, server_default="60",
    )
    cpu_core_seconds_per_credit_threshold = Column(
        Float, nullable=False, default=600.0, server_default="600",
    )
    ram_gb_seconds_per_credit_threshold = Column(
        Float, nullable=False, default=300.0, server_default="300",
    )
    io_mb_per_credit_threshold = Column(
        Float, nullable=False, default=1000.0, server_default="1000",
    )

    # Suppress alerts for users below this credit-usage floor in the
    # window. Prevents trivial free-tier consumption from drowning
    # the signal — a user who burned 0.1 credits AND 10 GPU-seconds
    # is technically over threshold but operationally noise.
    min_alert_credits = Column(Integer, nullable=False, default=10, server_default="10")

    enabled = Column(Boolean, nullable=False, default=True, server_default="true")

    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    audit_event_id = Column(BigInteger, ForeignKey("audit_events.id"), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    updated_by = relationship("User", foreign_keys=[updated_by_user_id])

    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_resource_monitor_config_singleton"),
        CheckConstraint(
            "gpu_seconds_per_credit_threshold > 0",
            name="ck_resource_monitor_config_gpu_threshold_pos",
        ),
        CheckConstraint(
            "cpu_core_seconds_per_credit_threshold > 0",
            name="ck_resource_monitor_config_cpu_threshold_pos",
        ),
        CheckConstraint(
            "ram_gb_seconds_per_credit_threshold > 0",
            name="ck_resource_monitor_config_ram_threshold_pos",
        ),
        CheckConstraint(
            "io_mb_per_credit_threshold > 0",
            name="ck_resource_monitor_config_io_threshold_pos",
        ),
        CheckConstraint(
            "min_alert_credits >= 0",
            name="ck_resource_monitor_config_min_alert_credits_nonneg",
        ),
    )

class BonusCouponDef(Base):
    """Admin-defined coupon that grants temporary extra credits OR
    extra storage to a tenant for a fixed duration. Not a Stripe
    coupon — Stripe's Coupon API is monetary-only. These are
    application-level perks tracked here + applied by the credit
    allotment + storage cap queries.

    See routers/admin_plans_payment_settings.create_bonus_coupon for
    the create flow + payment-settings/bonus-coupons/redeem for the
    admin-applied redemption flow.
    """
    __tablename__ = "bonus_coupon_defs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(80), nullable=False)
    code = Column(String(40), nullable=False, unique=True, index=True)
    # 'credits' adds N credits/month to the tenant's grant for
    # `duration_months`. 'storage' adds N GB to the tenant's cap.
    kind = Column(String(16), nullable=False)
    value = Column(Integer, nullable=False)
    duration_months = Column(Integer, nullable=False)
    max_redemptions = Column(Integer, nullable=True)
    times_redeemed = Column(Integer, nullable=False, default=0, server_default="0")
    active = Column(Boolean, nullable=False, default=True, server_default="t")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("kind IN ('credits', 'storage')",
                        name="ck_bonus_coupon_kind_enum"),
        CheckConstraint("value > 0",
                        name="ck_bonus_coupon_value_positive"),
        CheckConstraint("duration_months > 0 AND duration_months <= 120",
                        name="ck_bonus_coupon_duration_range"),
    )


class TenantBonusCoupon(Base):
    """One row per (tenant, bonus_coupon_def) redemption. The credit
    grant cron sums credits from active rows; the storage cap query
    sums storage from active rows. `active` is computed at query
    time as (starts_at <= now() < ends_at).
    """
    __tablename__ = "tenant_bonus_coupons"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    bonus_coupon_def_id = Column(Integer, ForeignKey("bonus_coupon_defs.id"), nullable=False)
    # Snapshot of the def at redemption time so changing the def
    # later doesn't retroactively alter active redemptions.
    kind = Column(String(16), nullable=False)
    value = Column(Integer, nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False, index=True)
    redeemed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    tenant = relationship("Tenant")
    bonus_coupon_def = relationship("BonusCouponDef")


class PlanListingInclude(SoftDeleteWithActorMixin, Base):
    """GG.UI.8c — generalized plan->catalog-listing inclusion.

    `PlanIncludedNode` (node-only) and `PlanPackage` (package/bundle-only)
    predate the unified GG.2 catalog and stay the source of truth for their
    own kinds (reconciler + entitlement expansion still read them). This
    table is the admin-facing surface added for GG.UI.8c so an admin can
    include ANY catalog listing kind (node/stack/workflow/bundle) in a plan
    without needing a package wrapper. `ref_id` is polymorphic like
    `PackageItem.ref_id` (node_definitions.id / stack listing id /
    workflow_listings.id / node_packages.id when kind='bundle') — no FK
    constraint since the target table differs per kind. Soft-delete only
    (ISO 27001, no hard deletes); `deleted_by_user_id` from the mixin
    records who removed an include for the audit trail.
    """
    __tablename__ = "plan_listing_includes"
    __table_args__ = (
        # GG.UI.8d — PARTIAL: only ACTIVE includes are unique. A plain
        # UNIQUE(plan_id, kind, ref_id) would make re-including a listing
        # after a soft-delete 500 (the soft-deleted row still holds the
        # slot). Mirrors the migration's
        # uq_plan_listing_include WHERE deleted_at IS NULL.
        Index("uq_plan_listing_include", "plan_id", "kind", "ref_id",
              unique=True, postgresql_where=text("deleted_at IS NULL")),
        CheckConstraint("kind IN ('node', 'stack', 'workflow', 'bundle')",
                        name="ck_plan_listing_include_kind"),
        Index("ix_plan_listing_includes_kind_ref", "kind", "ref_id"),
    )
    id = Column(Integer, primary_key=True)
    plan_id = Column(ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(20), nullable=False)
    ref_id = Column(Integer, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    plan = relationship("Plan")


class ListingCouponDef(Base):
    """GG.UI.8c — a coupon that targets ONE catalog listing (kind + ref_id)
    instead of a Stripe-wide money discount (`_MoneyCouponCreate` /
    `coupons_router.post ""`) or an app-level tenant perk
    (`BonusCouponDef`). Redeeming it at catalog checkout either grants the
    targeted listing free (`discount_type='free'`) or applies a % / fixed
    discount to that listing's quote only (never other lines in a
    bundle/workflow closure). Applied in
    `services/closure_fulfilment.apply_listing_coupon`, called from the
    `/catalog/{kind}/{id}/buy-with-credits` and `/checkout` routes.

    No hard deletes (ISO 27001): `active=False` retires a code the same
    way `BonusCouponDef.active` / `deactivate_bonus_coupon_def` do.
    """
    __tablename__ = "listing_coupon_defs"
    __table_args__ = (
        CheckConstraint("target_kind IN ('node', 'stack', 'workflow', 'bundle')",
                        name="ck_listing_coupon_target_kind"),
        CheckConstraint("discount_type IN ('free', 'percent', 'fixed')",
                        name="ck_listing_coupon_discount_type"),
        CheckConstraint(
            "(discount_type = 'free' AND discount_value IS NULL) OR "
            "(discount_type = 'percent' AND discount_value > 0 AND discount_value <= 100) OR "
            "(discount_type = 'fixed' AND discount_value > 0)",
            name="ck_listing_coupon_discount_value",
        ),
    )
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(80), nullable=False)
    code = Column(String(40), nullable=False, unique=True, index=True)
    target_kind = Column(String(20), nullable=False)
    target_ref_id = Column(Integer, nullable=False)
    discount_type = Column(String(16), nullable=False)
    # percent: 1-100 (%). fixed: cents off. free: NULL (unused).
    discount_value = Column(Integer, nullable=True)
    max_redemptions = Column(Integer, nullable=True)
    times_redeemed = Column(Integer, nullable=False, default=0, server_default="0")
    active = Column(Boolean, nullable=False, default=True, server_default="t")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LibraryProfile(Base):
    """Learned per-library facts (currently: GPU requirement).
    
    Keyed by lowercase canonical package name. Refreshed on access
    when older than 30 days; the curated seed list in
    `node_builder.gpu_library_index.CUDA_REQUIRED` is the fallback
    when network signals are unavailable.
    """
    __tablename__ = "library_profiles"

    name = Column(String(255), primary_key=True)
    gpu_required = Column(String(16), nullable=False)  # 'CPU' | 'CUDA' | 'ROCM' | 'UNKNOWN'
    evidence = Column(String(500), nullable=True)
    learned_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # wizard-knowledge-pack Phase 1 -- provenance of this row. Mirrors the
    # plain-String style of LibraryInstallRecipe.provenance below (not a
    # Postgres enum type). 'local_capture' | pack-supplied values.
    source = Column(String(32), nullable=False, default="local_capture", server_default="local_capture")
    # Which wizard-knowledge-pack version (if any) supplied this row; NULL
    # for locally captured rows.
    pack_version = Column(String(16), nullable=True)
    # Move C — known-working install steps per accelerator. Captured
    # after a successful smoke pass: the RUN/COPY block from the
    # working Dockerfile that installed this library, keyed by the
    # accelerator the build targeted. Consumed by the repair LLM
    # prompt for future builds importing the same library.
    # Shape: {"cuda": "RUN apt-get install -y g++ cmake\nRUN pip install nerfstudio==1.1.5", ...}
    # LEGACY, global, not read since GB.LEAK.5 — tenant-derived; superseded by
    # the per-tenant LibraryInstallRecipe table. Kept in place (no DROP).
    install_recipe_by_accel = Column(JSON, nullable=True)
    # Move D — install documentation excerpt scraped from the project's
    # README "Installation" section. Cold-start fallback for the repair
    # LLM when we don't yet have a known-working recipe captured.
    # Up to ~3KB; cached so we don't re-fetch GitHub READMEs on every
    # repair call. install_docs_fetched_at lets us refresh stale entries.
    install_docs_excerpt = Column(Text, nullable=True)
    install_docs_fetched_at = Column(DateTime(timezone=True), nullable=True)
    # Item #10 (fullproof extension) — recipe confidence scoring.
    # Map: accelerator -> count of successful builds whose install
    # included this library and converged on a working Dockerfile.
    # Higher count = more confidence the recipe is reliable. The
    # wizard UI surfaces "tested in N builds across the platform"
    # so users see whether they're benefiting from accumulated
    # community knowledge or trying something for the first time.
    # LEGACY, global, not read since GB.LEAK.5 — superseded by
    # LibraryInstallRecipe.success_count (per tenant). Kept in place (no DROP).
    successful_build_count_by_accel = Column(JSON, nullable=True)


class LibraryInstallRecipe(SoftDeleteWithActorMixin, Base):
    """GB.LEAK.3 — per-tenant known-working install recipe for a library.

    Replaces the tenant-derived global JSON columns on LibraryProfile
    (install_recipe_by_accel / successful_build_count_by_accel), which leaked
    one tenant's Dockerfile install block to every other tenant. One active
    row per (tenant_id, library_name, accelerator); soft-delete only.
    provenance: 'captured' | 'backfill_attributed' | 'legacy_unattributed'.
    Table created by migrations/20260925_library_install_recipes_per_tenant.py.
    """
    __tablename__ = "library_install_recipes"
    __table_args__ = (
        Index(
            "ix_library_install_recipe_unique_active",
            "tenant_id", "library_name", "accelerator",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    library_name = Column(String(255), nullable=False)
    accelerator = Column(String(32), nullable=False)
    recipe = Column(Text, nullable=False)
    success_count = Column(Integer, nullable=False, default=0, server_default="0")
    provenance = Column(String(32), nullable=False, default="captured", server_default="captured")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class WizardKnowledgePackVersion(Base):
    """wizard-knowledge-pack Phase 1 -- ledger of pack version install
    attempts (the internal design notes 1.3).

    One row per attempted/applied pack version. status: 'applied' |
    'rollback' | 'pending'. signature_status: 'valid' | 'invalid' |
    'unverified'. Table created by
    migrations/20261013h_wizard_knowledge_pack_schema.py.
    """
    __tablename__ = "wizard_knowledge_pack_versions"

    id = Column(BigInteger, primary_key=True)
    version = Column(String(16), nullable=False)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    applied_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(16), nullable=False, default="pending", server_default="pending")
    integrity_check_payload = Column(JSONB, nullable=True)
    source_url = Column(String(500), nullable=True)
    signature_status = Column(String(16), nullable=False, default="unverified", server_default="unverified")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# MOD Assistant (mod-assistant-agent §1) — chat-agent session + message log
# + per-tenant config. Three tables, all additive.
#
# Sessions are per-tab (the launcher mints a fresh session id in
# sessionStorage on first open) and expire on the per-tenant
# session_retention_hours boundary (default 1h idle, max 168 = 1 week).
# AssistantMessage.actions_json holds the structured action emits the
# orchestrator validated and forwarded to the client. AssistantTenantConfig
# is one row per tenant, backfilled at startup by _migrate_schema() so
# every existing tenant lands with sane defaults.
# ---------------------------------------------------------------------------


class AssistantSession(Base):
    """One chat session per browser tab. PK is a UUID surfaced as the
    `session_id` header on every POST /assistant/chat request — clients
    mint it in sessionStorage on first open. last_active_at is bumped
    on every turn; the hourly expiry sweep deletes rows whose
    last_active_at is older than the owning tenant's retention window.
    """

    __tablename__ = "assistant_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_active_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    # User's location at the moment the turn was submitted. Lets the
    # orchestrator ground open-dialog ids against the right page in the
    # site map without the client having to repeat the page on every turn.
    current_page = Column(String(255), nullable=True)
    current_workflow_id = Column(Integer, ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True)
    model_id = Column(String(128), nullable=True)
    # GE.MAA.1 — soft-end marker for POST /assistant/session/end. NULL means
    # still active. Never a row delete (ISO 27001): the hourly expiry sweep
    # already hard-deletes on retention timeout (5.8, pre-existing), but an
    # explicit user-initiated end must leave the row + transcript queryable.
    ended_at = Column(DateTime(timezone=True), nullable=True)

    messages = relationship(
        "AssistantMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AssistantMessage.created_at",
    )


class AssistantMessage(Base):
    """One row per chat turn. role='user' for user messages,
    'assistant' for the model's final response, 'tool' for
    intermediate tool-call results (site map lookup, RAG, logs). Cost
    telemetry (prompt_tokens / completion_tokens / latency_ms) is
    populated for assistant rows only, from the LLM provider's response
    metadata; rows whose provider failed to emit usage stats persist
    NULL with a logged warning at orchestrator time.
    """

    __tablename__ = "assistant_messages"

    id = Column(UUID(as_uuid=True), primary_key=True)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("assistant_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=True)
    # Structured emits the orchestrator forwarded to the client (after
    # schema + allowlist + site-map validation). Empty list when the
    # assistant produced text only. Shape: [{"action": "navigate",
    # "url": "/builder/new"}, ...].
    actions_json = Column(PostgresqlJSONB, nullable=False, server_default="[]")
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    model_id = Column(String(128), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    session = relationship("AssistantSession", back_populates="messages")

    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant', 'tool')",
            name="ck_assistant_message_role_enum",
        ),
    )


class AssistantTenantConfig(Base):
    """Per-tenant agent settings. Exactly one row per tenant; backfilled
    by database._migrate_schema() at startup so every tenant — current
    or future — has defaults to fall back on. Fields left NULL mean
    'use the platform default' so the platform-admin override surface
    can clear an override without re-implementing every default in
    every row.
    """

    __tablename__ = "assistant_tenant_config"

    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled = Column(Boolean, nullable=False, default=True, server_default="t")
    model_id = Column(String(128), nullable=True)
    system_prompt_override = Column(Text, nullable=True)
    # JSONB map of action name -> bool. Missing keys are interpreted as
    # 'allowed' (the orchestrator default), so a tenant that disables
    # only one action (e.g. open-new-tab) doesn't need to enumerate the
    # entire vocabulary. Validated against the action vocabulary at
    # write time by the admin endpoint.
    action_allowlist = Column(PostgresqlJSONB, nullable=False, server_default="{}")
    logs_context_enabled = Column(Boolean, nullable=False, default=True, server_default="t")
    session_retention_hours = Column(Integer, nullable=False, default=1, server_default="1")
    knowledge_base_enabled = Column(Boolean, nullable=False, default=False, server_default="f")
    last_modified_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_modified_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "session_retention_hours >= 1 AND session_retention_hours <= 168",
            name="ck_assistant_tenant_retention_hours_range",
        ),
    )


class AiSlotBinding(Base):
    """ai-config-stack-binding 1.1 (design D1) — one row per (slot, scope,
    priority).

    The binding of an AI role (wizard/adapter/assistant/support/repair/critic,
    plus the embedding slots wizard_learning_embed/assistant_user_rag_embed/
    assistant_knowledge_embed) to a target lives in a QUERYABLE table, not in
    the Vault tool-config blob:
    "which slots point at stack 42?" is a SQL read, and tenant-over-platform
    precedence resolves in SQL, not by reading six Vault paths per request.
    Vault keeps the secret (`key_ref` points at a Vault path — never a secret)
    and the non-binding knobs (temperature, max_tokens).

    SOFT DELETE ONLY (ISO 27001 A.18.1.3 — records shall not be destroyed): a
    retired binding sets `enabled=false` and stamps `retired_at`; the row is
    NEVER hard-deleted, so the history of "this slot once ran on that stack"
    survives. The FKs carry no ON DELETE CASCADE: deleting a tenant, a stack or
    a user must never erase the binding record.

    NEW TABLE ONLY (models.py is bind-mounted live and shared by every lane:
    no column was added to any existing table).
    """

    __tablename__ = "ai_slot_binding"

    # BigInteger/Integer variant (same trick as AuditEvent) so the test
    # suite's engine autoincrements the key while Postgres keeps BIGSERIAL.
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The AI role this row binds: wizard|adapter|assistant|support|repair|critic,
    # or an embedding slot (wizard_learning_embed|assistant_user_rag_embed|
    # assistant_knowledge_embed).
    slot = Column(String(28), nullable=False, index=True)
    # 'platform' | 'tenant' — tenant rows override the platform row for that slot.
    scope = Column(String(8), nullable=False, index=True)
    # The tenant whose override this is; NULL when scope='platform'. FK WITHOUT
    # ON DELETE CASCADE: the binding record survives tenant deletion.
    tenant_id = Column(ForeignKey("tenants.id", ondelete="SET NULL"),
                       nullable=True, index=True)
    # 'stack' (a PersistentStack serves the slot) | 'external' (a base_url) |
    # 'auto' (no container exists today — the resolver must never bind a stack
    # that does not exist).
    target_kind = Column(String(8), nullable=False, index=True)
    # The serving stack when target_kind='stack'. FK ON DELETE SET NULL (two
    # words) + denormalized name so a deleted stack keeps the row readable.
    stack_id = Column(Integer, ForeignKey("persistent_stacks.id",
                                          ondelete="SET NULL"),
                      nullable=True, index=True)
    stack_name = Column(String(255), nullable=True)
    # External targets only — the host:port the slot dials.
    base_url = Column(String(512), nullable=True)
    model = Column(String(128), nullable=True)
    # A Vault PATH (e.g. platform/ai-config/assistant), NEVER the secret itself.
    key_ref = Column(String(512), nullable=True)
    # What kind of model the slot needs: 'chat' (generation) | 'embedding'.
    # The three *_embed slots require 'embedding'; every other slot requires
    # 'chat' (ck_ai_slot_binding_capability_matches_slot).
    capability = Column(String(16), nullable=False, default="chat",
                        server_default=text("'chat'"), index=True)
    # Position in the slot's ordered fallback chain (owner BUG.B56): 0 = primary,
    # 1..15 = fallbacks tried in order after a failure.
    priority = Column(SmallInteger, nullable=False, default=0,
                      server_default="0", index=True)
    # Soft-delete switch: false = retired, never a DELETE.
    enabled = Column(Boolean, nullable=False, default=True,
                     server_default="true", index=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())
    # NULL while live; stamped when the binding is retired (enabled=false).
    retired_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id",
                                                    ondelete="SET NULL"),
                                nullable=True, index=True)

    __table_args__ = (
        CheckConstraint(
            "slot IN ('wizard', 'adapter', 'assistant', 'support', "
            "'repair', 'critic', 'wizard_learning_embed', "
            "'assistant_user_rag_embed', 'assistant_knowledge_embed')",
            name="ck_ai_slot_binding_slot",
        ),
        CheckConstraint(
            "capability IN ('chat', 'embedding')",
            name="ck_ai_slot_binding_capability",
        ),
        # The three *_embed slots are the only embedding slots; every other slot
        # is a chat/generation slot (owner BUG.B57/B58/B59).
        CheckConstraint(
            "(slot IN ('wizard_learning_embed', 'assistant_user_rag_embed', "
            "'assistant_knowledge_embed') AND capability = 'embedding') OR "
            "(slot NOT IN ('wizard_learning_embed', 'assistant_user_rag_embed', "
            "'assistant_knowledge_embed') AND capability = 'chat')",
            name="ck_ai_slot_binding_capability_matches_slot",
        ),
        CheckConstraint(
            "scope IN ('platform', 'tenant')",
            name="ck_ai_slot_binding_scope",
        ),
        CheckConstraint(
            "target_kind IN ('stack', 'external', 'auto')",
            name="ck_ai_slot_binding_target_kind",
        ),
        # scope and tenant_id agree: platform rows own no tenant.
        CheckConstraint(
            "(scope = 'platform' AND tenant_id IS NULL) OR "
            "(scope = 'tenant' AND tenant_id IS NOT NULL)",
            name="ck_ai_slot_binding_scope_tenant",
        ),
        # A stack target names the stack; the others name their own transport.
        CheckConstraint(
            "target_kind != 'stack' OR stack_id IS NOT NULL",
            name="ck_ai_slot_binding_stack_target_names_stack",
        ),
        CheckConstraint(
            "target_kind != 'external' OR base_url IS NOT NULL",
            name="ck_ai_slot_binding_external_target_names_base_url",
        ),
        # A disabled row carries the clock that disabled it ...
        CheckConstraint(
            "enabled OR retired_at IS NOT NULL",
            name="ck_ai_slot_binding_retirement_clock",
        ),
        # ... and a row stamped retired is disabled (reviving means a NEW row).
        CheckConstraint(
            "retired_at IS NULL OR NOT enabled",
            name="ck_ai_slot_binding_retired_is_disabled",
        ),
        # Fallback-chain positions are bounded (owner BUG.B56): 0 = primary,
        # 1..15 = the ordered fallbacks tried after a failure.
        CheckConstraint(
            "priority >= 0 AND priority <= 15",
            name="ck_ai_slot_binding_priority_range",
        ),
        # One ENABLED row per (slot, scope, tenant_id, priority): the chain is
        # keyed by position, so a slot may hold #0, #1, #2 ... but never two
        # enabled rows at the same position. NULL tenant_id means platform, so
        # the partial index coalesces it to 0. Tombstones (retired rows) are
        # excluded, so retiring a binding frees that position for a new pick
        # without colliding with its own retired row.
        Index("uq_ai_slot_binding_live",
              "slot", "scope",
              func.coalesce(tenant_id, 0),
              "priority",
              unique=True,
              postgresql_where=text("enabled IS TRUE")),
        Index("ix_ai_slot_binding_slot_scope", "slot", "scope"),
        # Chain resolution reads: "give me this slot's enabled rows in order".
        Index("ix_ai_slot_binding_slot_scope_tenant_priority",
              "slot", "scope", "tenant_id", "priority"),
    )


class AiKnowledgeSourceBinding(Base):
    """owner BUG.B59 (Q-B59=A) — assistant knowledge = a PICKER over knowledge
    sources, each with its OWN embedding model.

    The assistant_knowledge_embed slot (services/ai_slot_resolver.py EMBEDDING_SLOTS)
    is one model for the whole knowledge base. This table splits that per SOURCE:
    MOD Docs, a tenant's own files, and tenant uploads are retrieved with DIFFERENT
    embedding models, so a query embeds with the model bound to the source it
    searches. One row per (scope, tenant, source_kind): the picker the UI and the
    endpoints will offer.

    `source_kind` mirrors the ingestion kinds that actually exist in code
    (services/assistant_knowledge_service.py: create_upload_document ->
    source_type='upload', create_text_document -> 'paste', create_folder_documents
    -> 'folder', i.e. a tenant's existing storage folders/Cogs), plus 'mod_docs',
    the platform documentation corpus the assistant's retrieve_docs tool reads.

    The embedding target columns are AiSlotBinding's verbatim (target_kind
    stack|external|auto, stack_id FK SET NULL + denormalized stack_name, base_url,
    model, served_model, key_ref) so the resolver code that already understands
    that shape understands this one too.

    SOFT DELETE ONLY (ISO 27001 A.18.1.3): retiring a source sets enabled=false and
    stamps retired_at; the row is NEVER hard-deleted, so "this tenant once embedded
    its uploads with that model" survives. FKs carry no ON DELETE CASCADE: deleting
    a tenant/stack/user must never erase the binding record.

    NEW TABLE ONLY (models.py is bind-mounted live and shared by every lane: no
    column was added to any existing table).
    """

    __tablename__ = "ai_knowledge_source_binding"

    # BigInteger/Integer variant (same trick as AuditEvent) so the test suite's
    # engine autoincrements the key while Postgres keeps BIGSERIAL.
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # 'platform' | 'tenant' — tenant rows override the platform row for a kind.
    scope = Column(String(8), nullable=False, index=True)
    # The tenant whose override this is; NULL when scope='platform'. FK WITHOUT
    # ON DELETE CASCADE: the binding record survives tenant deletion.
    tenant_id = Column(ForeignKey("tenants.id", ondelete="SET NULL"),
                       nullable=True, index=True)
    # 'mod_docs' (MOD's documentation corpus) | 'tenant_files' (the tenant's
    # existing storage folders/Cogs, ingested via /folder -> source_type='folder')
    # | 'uploads' (documents a tenant uploaded or pasted: source_type
    # 'upload'|'paste').
    source_kind = Column(String(16), nullable=False, index=True)
    # Soft-delete switch: false = retired, never a DELETE.
    enabled = Column(Boolean, nullable=False, default=True,
                     server_default="true", index=True)
    # 'stack' (a PersistentStack serves the source) | 'external' (a base_url) |
    # 'auto' (no container exists today — the resolver must never bind a stack
    # that does not exist).
    target_kind = Column(String(8), nullable=False, index=True)
    # The serving stack when target_kind='stack'. FK ON DELETE SET NULL (two
    # words) + denormalized name so a deleted stack keeps the row readable.
    stack_id = Column(Integer, ForeignKey("persistent_stacks.id",
                                          ondelete="SET NULL"),
                      nullable=True, index=True)
    stack_name = Column(String(255), nullable=True)
    # The model id the target actually serves (AiSlotBinding's served_model).
    served_model = Column(String(128), nullable=True)
    # External targets only — the host:port the source dials.
    base_url = Column(String(512), nullable=True)
    model = Column(String(128), nullable=True)
    # A Vault PATH (e.g. platform/ai-config/knowledge-embed), NEVER the secret.
    key_ref = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())
    # NULL while live; stamped when the binding is retired (enabled=false).
    retired_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id",
                                                    ondelete="SET NULL"),
                                nullable=True, index=True)

    # Derived names use the `akb` short form: Postgres truncates identifiers at
    # 31 bytes, and `ix_` + this 27-char table name already leaves 1 byte, so the
    # full-name style AiSlotBinding uses would collide after truncation.
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('mod_docs', 'tenant_files', 'uploads')",
            name="ck_akb_source_kind",
        ),
        CheckConstraint(
            "scope IN ('platform', 'tenant')",
            name="ck_akb_scope",
        ),
        CheckConstraint(
            "target_kind IN ('stack', 'external', 'auto')",
            name="ck_akb_target_kind",
        ),
        # scope and tenant_id agree: platform rows own no tenant.
        CheckConstraint(
            "(scope = 'platform' AND tenant_id IS NULL) OR "
            "(scope = 'tenant' AND tenant_id IS NOT NULL)",
            name="ck_akb_scope_tenant",
        ),
        # A stack target names the stack; the others name their own transport.
        CheckConstraint(
            "target_kind != 'stack' OR stack_id IS NOT NULL",
            name="ck_akb_stack_target_names_stack",
        ),
        CheckConstraint(
            "target_kind != 'external' OR base_url IS NOT NULL",
            name="ck_akb_ext_target_names_base_url",
        ),
        # A disabled row carries the clock that disabled it ...
        CheckConstraint(
            "enabled OR retired_at IS NOT NULL",
            name="ck_akb_retirement_clock",
        ),
        # ... and a row stamped retired is disabled (reviving means a NEW row).
        CheckConstraint(
            "retired_at IS NULL OR NOT enabled",
            name="ck_akb_retired_is_disabled",
        ),
        # The picker holds at most ONE live row per (scope, tenant, kind): a
        # source has one embedding model at a time. NULL tenant_id means platform,
        # so the partial index coalesces it to 0. Tombstones (retired rows) are
        # excluded, so retiring a source frees its kind for a new pick without
        # colliding with its own retired row.
        Index("uq_akb_live",
              "scope",
              func.coalesce(tenant_id, 0),
              "source_kind",
              unique=True,
              postgresql_where=text("retired_at IS NULL")),
        Index("ix_akb_scope", "scope"),
        Index("ix_akb_tenant_id", "tenant_id"),
        Index("ix_akb_source_kind", "source_kind"),
        Index("ix_akb_stack_id", "stack_id"),
        Index("ix_akb_enabled", "enabled"),
        Index("ix_akb_created_at", "created_at"),
        Index("ix_akb_retired_at", "retired_at"),
        Index("ix_akb_created_by_user_id", "created_by_user_id"),
        # Picker read: "give me this scope/tenant's live sources in order".
        Index("ix_akb_scope_tenant_kind", "scope", "tenant_id", "source_kind"),
    )


class TenantKnowledgeDocument(SoftDeleteMixin, Base):
    """One ingested source in a tenant's assistant knowledge base
    (tenant-assistant-knowledge-base). Added by a tenant admin from an upload,
    pasted text, or an existing storage folder, then chunked + embedded into the
    tenant's own `t{tenant_id}_vectors` collection. Every chunk carries payload
    `source="kb"` + this row's id, so a single document can be deleted or
    reindexed by payload filter without touching the rest of the collection.

    `status` tracks the background ingest lifecycle
    (pending → processing → ready | failed); `chunk_count` reflects the embedded
    chunks on success and `error_message` the reason on failure. `tenant_id`
    always comes from the authenticated principal, never the request body.
    """

    __tablename__ = "tenant_knowledge_documents"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    source_type = Column(String(16), nullable=False)
    title = Column(String(512), nullable=False)
    file_id = Column(String, ForeignKey("files.id", ondelete="SET NULL"), nullable=True)
    cog_id = Column(Integer, ForeignKey("cogs.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(16), nullable=False, default="pending", server_default="pending")
    chunk_count = Column(Integer, nullable=False, default=0, server_default="0")
    bytes = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_by_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())

    tenant = relationship("Tenant")


class StackProfile(Base):
    """One row per managed stack — the measured sibling of AcceleratorProfile.

    Where AcceleratorProfile characterizes a physical device, this characterizes a
    managed PersistentStack: how long it takes to warm cold->serving, its idle and
    serving VRAM footprint, whether its readiness probe is trustworthy, and whether
    a correctness smoke test passed (stack-training-and-prewarm §Gate 0).

    Keyed by stack_name (the managed stacks are platform-owned singletons under one
    tenant, and every wake/sleep path already identifies a stack by name), with an
    optional stack_id back-reference for convenience. The warm-time statistic is a
    p90 computed from a bounded rolling window of the most recent observed wake
    durations (warm_samples); `samples` is the lifetime count of observations.
    """
    __tablename__ = "stack_profiles"

    id = Column(Integer, primary_key=True, index=True)
    # Identity is the stack NAME (one profile per managed stack); stack_id is an
    # optional back-reference to the PersistentStack row for joins/debugging.
    stack_name = Column(String(255), nullable=False, unique=True, index=True)
    stack_id = Column(Integer, ForeignKey("persistent_stacks.id"), nullable=True, index=True)

    # Rolling p90 of measured cold->serving warm time, folded from real wakes.
    warm_time_p90_s = Column(Float, nullable=True)
    idle_vram_gb = Column(Float, nullable=True)
    serving_vram_gb = Column(Float, nullable=True)

    # Whether the declared readiness probe was proven trustworthy by the smoke test.
    probe_valid = Column(Boolean, nullable=True)
    # 'passed' | 'failed' | 'unknown' (characterization outcome).
    test_status = Column(String(16), default="unknown", server_default="unknown", nullable=False)
    test_reason = Column(Text, nullable=True)
    last_tested_at = Column(DateTime(timezone=True), nullable=True)

    # Lifetime count of warm-time observations folded in.
    samples = Column(Integer, default=0, server_default="0", nullable=False)
    # Bounded rolling window of the most recent observed warm-times (seconds), the
    # source the p90 is recomputed from. Kept small (WARM_SAMPLE_WINDOW) so the
    # estimate tracks current cache/hardware state rather than ancient cold starts.
    warm_samples = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)


# ============================================================================
# 27.6.s2 (launch-completion-megaplan, gr-llm-serving) — bench results.
# The worker's `bench` stack op (Worker/internal/executor/stack_bench.go)
# measures what a deployed stack SERVES — TTFT per prompt size, decode tok/s
# per concurrency, cache-hit TTFT — and rides the report out of the task log
# tail between MOD_STACK_BENCH sentinels. 27.6 says those results go to
# stack_profiles and feed the admission limits in 27.4. StackProfile itself
# is one-row-per-stack and gets folded by the calibration path, so the bench
# gets its own APPEND-ONLY ledger row (ISO 27001: a measurement never
# overwrites a measurement) keyed to stack + stack-revision + worker.
# ============================================================================


class StackBenchProfile(Base):
    """One append-only bench measurement of a persistent stack.

    Keyed to (stack, revision_no, worker): the same stack benched after an
    edit, or on a different host, is a different row — capacity is a property
    of the composition AND the machine that served it. Nothing here is ever
    mutated; the newest row for a stack is what readers take.
    """
    __tablename__ = "stack_bench_profiles"
    __table_args__ = (
        Index("ix_stack_bench_profiles_stack", "stack_id"),
    )

    id = Column(BigInteger, primary_key=True, index=True)
    stack_id = Column(ForeignKey("persistent_stacks.id", ondelete="SET NULL"),
                      nullable=True, index=True)
    # Denormalized so the row survives its stack's teardown (same convention
    # as PersistentStackRevision).
    stack_name = Column(String(255), nullable=True)
    # The stack's current revision_no at bench time (0 = never edited —
    # revisions are materialized lazily).
    revision_no = Column(Integer, nullable=False, default=0,
                         server_default="0")
    # The worker that ran the sweep (claim-time host, not the guess).
    worker_name = Column(String(255), nullable=True)
    # The model id the bench discovered/swept, when the worker reported one.
    model = Column(String(255), nullable=True)
    # The worker's report verbatim: {"ttft_ms": {"8000": 123.4, ...},
    # "decode_tps": {"1": 31.2, ...}, "cache_hit_ttft_ms": 45.6,
    # "model": "...", "errors": [...]}. Keys omitted, never zero-filled —
    # a size that timed out has no number.
    report = Column(JSONB, nullable=True)
    # Short error labels the sweep carried (size/concurrency/reason only).
    errors = Column(JSONB, nullable=True)
    benched_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)


# Gate 11 step 2 — declare which CPU architectures a task's image can run on.
# Registered here, once, rather than at the 31 sites that create a task row:
# those keep multiplying, and a rule enforced in one place holds for code
# nobody has written yet. The hook is memory-only; see utils/image_arch.py.
try:
    from sqlalchemy import event as _sa_event
    from utils.image_arch import stamp_task_arches as _stamp_task_arches

    _sa_event.listen(ApiTaskQueue, "before_insert", _stamp_task_arches)
except Exception:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger(__name__).debug(
        "image-arch stamping not registered", exc_info=True)


# ============================================================================
# GG.6b (launch-completion-megaplan, gg-commerce) — redeemable listing codes.
# A code hands an entitlement (node/stack/workflow/bundle) to a tenant with
# source 'invite' or 'trial'. Only the sha256 HASH is stored — the plaintext
# is shown exactly once at creation and never persisted (so a DB leak cannot
# redeem codes). Soft delete only (ISO 27001 — no hard deletes); the
# append-only `listing_code_redemptions` ledger carries a
# unique (code_id, tenant_id) so one redemption per tenant holds even under
# concurrency.
# ============================================================================

class ListingCode(SoftDeleteMixin, Base):
    """GG.6b — a redeemable code for a listing entitlement."""
    __tablename__ = "listing_codes"
    __table_args__ = (
        UniqueConstraint("code_hash", name="uq_listing_codes_code_hash"),
        Index("ix_listing_codes_kind", "kind"),
        # deleted_at is already indexed by SoftDeleteMixin (index=True).
    )
    id = Column(Integer, primary_key=True)
    # sha256(plaintext code), hex — the ONLY stored form of the code.
    code_hash = Column(String(64), nullable=False)
    # 'node' | 'stack' | 'workflow' | 'bundle'
    kind = Column(String(20), nullable=False)
    item_id = Column(Integer, nullable=False)
    # 'invite' | 'trial' — becomes the entitlement row's `source` on redeem.
    source = Column(String(20), nullable=False)
    # Days the granted entitlement lasts; NULL = permanent grant.
    grant_days = Column(Integer, nullable=True)
    # NULL = unlimited redemptions.
    max_redemptions = Column(Integer, nullable=True)
    redemption_count = Column(Integer, nullable=False, default=0, server_default="0")
    # The code's own expiry (independent of the grant it issues).
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ListingCodeSecret(Base):
    """BUG.B19 — the redeemable code, ENCRYPTED with the owning tenant's key
    (services/http_triggers.encrypt_secret pattern), so the seller can view
    and copy it again later. code_hash on listing_codes stays the redemption
    lookup. One row per code; never hard-deleted (ISO 27001), the code's
    soft delete/disable governs it."""
    __tablename__ = "listing_code_secrets"
    id = Column(Integer, primary_key=True)
    code_id = Column(Integer, ForeignKey("listing_codes.id"), nullable=False, unique=True, index=True)
    tenant_id = Column(Integer, nullable=False, index=True)  # tenant whose key encrypted it
    code_enc = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MarketplaceWebhookEvent(Base):
    """MKT.C2 — idempotency ledger for the Marketplace purchase-delivery
    webhook (``POST /v1/marketplace/purchase-webhook``).

    One row per ``sale_id``: a replayed sale finds its row and returns the
    ORIGINAL result stored in ``response_body`` without touching any
    entitlement (idempotent on sale_id, the webhook's natural key).

    ISO 27001: rows are NEVER deleted — a soft ``revoked`` status marks a
    sale whose items were later revoked.
    """
    __tablename__ = "marketplace_webhook_events"
    __table_args__ = (
        UniqueConstraint("sale_id", name="uq_marketplace_webhook_event_sale_id"),
        Index("ix_marketplace_webhook_events_tenant", "tenant_id"),
    )
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    sale_id = Column(String(255), nullable=False, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=True, index=True)
    # JSON-encoded HTTP response body returned for the FIRST (original)
    # delivery — replays return it byte-for-byte so the Marketplace's
    # retry logic converges on one answer.
    response_body = Column(JSONB, nullable=False, default=dict)
    status = Column(String(20), nullable=False,
                    default="processed", server_default="processed")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())


class MarketplaceCreditSaleReport(Base):
    """GG.MKT.CR (launch-completion-megaplan) — outbox row reporting a
    credit-funded Core purchase (``POST /catalog/{kind}/{id}/buy-with-credits``)
    to the Marketplace as a credit sale, PURELY so the publisher's revenue
    share and payouts count it (Core deducts + grants the entitlement
    itself, synchronously, before this row is even written — the
    Marketplace is never involved in fulfilment for credit purchases).

    Outbox pattern: written synchronously (never blocking/rolling back the
    purchase) in ``services/closure_fulfilment.py:buy_with_credits``; sent
    later by the ``services/marketplace_credit_sale_report.py`` scheduler
    job (gated on the ``marketplace.credit_sale_report_enabled`` platform
    setting). ``core_purchase_id`` is ``ClosurePurchase.payment_ref``
    (``closure:ref=<tenant_id>:<idempotency_key>``) — unique per purchase,
    so this outbox row (and the Marketplace's own ``Sale.idempotency_key``
    dedup on the same value) are both idempotent by construction.

    ISO 27001: append-only — rows are NEVER deleted, only transitioned
    pending -> sent|failed (a ``failed`` row may still be retried later;
    ``attempts``/``last_error`` track that without losing history).
    """
    __tablename__ = "marketplace_credit_sale_reports"
    __table_args__ = (
        UniqueConstraint("core_purchase_id", name="uq_marketplace_credit_sale_report_purchase_id"),
        Index("ix_marketplace_credit_sale_reports_status", "status"),
        Index("ix_marketplace_credit_sale_reports_tenant", "tenant_id"),
    )
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    core_purchase_id = Column(String(255), nullable=False)
    kind = Column(String(32), nullable=False)
    core_ref = Column(Integer, nullable=False)
    credits = Column(Integer, nullable=False)
    credit_unit_price_micros = Column(BigInteger, nullable=False)
    # pending -> sent | failed (failed rows may be retried -> sent).
    status = Column(String(20), nullable=False, default="pending", server_default="pending")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)


class ListingCodeRedemption(Base):
    """GG.6b — append-only redemption ledger (one row per (code, tenant))."""
    __tablename__ = "listing_code_redemptions"
    __table_args__ = (
        UniqueConstraint("code_id", "tenant_id", name="uq_listing_code_redemption_code_tenant"),
        Index("ix_listing_code_redemptions_code", "code_id"),
        Index("ix_listing_code_redemptions_tenant", "tenant_id"),
    )
    id = Column(Integer, primary_key=True)
    code_id = Column(Integer, ForeignKey("listing_codes.id"), nullable=False)
    tenant_id = Column(Integer, nullable=False)
    user_id = Column(Integer, nullable=True)
    redeemed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TenantBurstAccount(SoftDeleteMixin, Base):
    """GH.2 — per-tenant cloud account for bursting into the CUSTOMER's own
    cloud (launch-completion-megaplan group H, decomp/gh-burst.md).

    The account references the tenant's cloud credential in the EXISTING
    tenant secret store by NAME only (`secret_ref` = the secret's name /
    Vault path). The credential VALUE never lives in this table, so a
    leaked row is not a leaked credential. `limits` is a JSONB policy
    block: {max_instances, max_gpu_hours_month, allowed_instance_types}
    (GH.4 metering + GH.5 scale controller consume it).

    `status` tracks the last dry-run permission probe (POST
    /burst/accounts/{id}/probe): 'pending' (never probed), 'valid'
    (list_tagged succeeded), 'invalid' (probe failed; `last_probe_error`
    holds a redacted message — never the credential).

    ISO 27001: soft-delete only (deleted_at); rows are never hard-deleted
    and every mutation is audit-logged. One LIVE account per
    (tenant, provider): the partial unique index excludes soft-deleted
    rows so a re-created account can reuse the provider name.
    """
    __tablename__ = "tenant_burst_accounts"
    __table_args__ = (
        # Partial unique index: exactly one ACTIVE account per
        # (tenant_id, provider). Soft-deleted (deleted_at IS NOT NULL)
        # rows are outside the index, so delete-then-recreate works.
        Index(
            "uq_tenant_burst_account_live_provider",
            "tenant_id", "provider",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    # Registered burst-provider name (burst_provider.get_provider key):
    # 'fake' in dev/tests; 'libcloud' for the real clouds (GH.7).
    provider = Column(String(50), nullable=False)
    # NAME of the tenant secret holding the cloud credential — never the
    # credential itself. Resolved at probe/provision time via the existing
    # tenant secret store (crud.secret_cud.resolve_decrypted_secret_for_tenant).
    secret_ref = Column(String(255), nullable=False)
    region = Column(String(100), nullable=True)
    # JSONB policy block: {max_instances, max_gpu_hours_month,
    # allowed_instance_types}. Plain JSONB (no .astext anywhere).
    limits = Column(JSONB, nullable=True, default=dict)
    # 'pending' | 'valid' | 'invalid' — result of the last permission probe.
    status = Column(String(20), nullable=False,
                    default="pending", server_default="pending")
    last_probe_at = Column(DateTime(timezone=True), nullable=True)
    # Redacted probe failure (credential values stripped before storage).
    last_probe_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)


class PlatformSetting(Base):
    """GS.PSR — DB-backed platform-settings registry (tunables + feature
    flags moving OUT of env into the DB, per [[settings-not-env-files]]).

    A single row per (key, scope, tenant) triple is LIVE at a time; a
    `set_setting` soft-supersedes the old row (stamps `deleted_at`) and
    inserts a fresh one, so the full history of every change is retained
    (ISO 27001: NO hard deletes — see [[iso-no-hard-delete]]). The
    append-only `platform_settings_audit` table mirrors each mutation for
    the audit trail.

    Resolution (see services.platform_settings.get_setting):
    tenant row > platform row > env fallback > default.

    `value` is JSONB and is coerced to the registered Python type on read
    (NEVER read via `.astext` — see the JSONB rule).
    """
    __tablename__ = "platform_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(Text, nullable=False)
    scope = Column(String(16), nullable=False, default="platform",
                   server_default="platform")
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    value = Column(JSONB, nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "scope IN ('platform', 'tenant')",
            name="ck_platform_settings_scope",
        ),
        CheckConstraint(
            # A tenant-scoped row must belong to a tenant; a platform row
            # must not. Keeps the (key, scope, tenant) triple unambiguous.
            "(scope = 'tenant') = (tenant_id IS NOT NULL)",
            name="ck_platform_settings_scope_tenant",
        ),
    )

    # Live-row uniqueness: at most one live row per (key, scope, tenant).
    # Superseded (deleted_at-set) rows fall outside the partial index, so
    # history can accumulate freely. tenant_id is INTEGER here (matches
    # tenants.id), so COALESCE(tenant_id, 0) gives a stable platform bucket.
    Index(
        "uq_platform_settings_live",
        "key", "scope", text("COALESCE(tenant_id, 0)"),
        unique=True,
        postgresql_where=text("deleted_at IS NULL"),
    ),
    Index(
        "ix_platform_settings_live_lookup",
        "key", "scope", "tenant_id",
        postgresql_where=text("deleted_at IS NULL"),
    ),


class PlatformSettingAudit(Base):
    """GS.PSR — append-only audit trail for platform-settings changes.

    One row per `set_setting` mutation capturing the before/after value
    and the actor. Appended by services.platform_settings.set_setting in
    the SAME transaction as the soft-supersede, so a change and its audit
    entry commit together or not at all.

    ENFORCED append-only in production by BEFORE UPDATE OR DELETE triggers
    installed by the migration (see [[iso-no-hard-delete]]). `Base.metadata
    .create_all` does NOT install triggers, so a sibling test DB must add
    them explicitly to exercise the guard (see tests).
    """
    __tablename__ = "platform_settings_audit"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(Text, nullable=False)
    scope = Column(String(16), nullable=False, default="platform",
                   server_default="platform")
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    old_value = Column(JSONB, nullable=True)
    new_value = Column(JSONB, nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    changed_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)

    __table_args__ = (
        CheckConstraint(
            "scope IN ('platform', 'tenant')",
            name="ck_platform_settings_audit_scope",
        ),
    )
    Index(
        "ix_platform_settings_audit_lookup",
        "key", "scope", "tenant_id",
    ),


class BurstSession(Base):
    """GH.4 — one row per burst worker's lifetime, from join (first
    heartbeat/enrollment) to leave (drained + torn down), for
    launch-completion-megaplan group H (decomp/gh-burst.md).

    This is the burst-hour METER: `joined_at` → `left_at` brackets the
    session, `last_debited_at` is the idempotency watermark (mirrors
    `PersistentStack.metered_through_at` in stack_billing.py) advanced in
    the SAME transaction as the per-tick debit so a crash/re-run never
    double-charges, and `credits_debited_total` is the running sum for
    the audit trail / dashboards. `gpu_class` feeds
    `burst_pricing.burst_fee_credits_per_hour` for the per-hour rate.

    `status`: 'active' (metering normally) -> 'draining' (balance hit
    zero; teardown requested/in flight, no further debits) -> 'closed'
    (left_at set; worker gone, whether by graceful leave or forced
    teardown). This is live metering STATE, not a ledger — the append-only
    audit trail is `CreditLedgerEntry` (entry_type=BURST_MANAGEMENT_FEE)
    for the money and `AuditEvent` for hold refusals / teardown actions;
    this row is mutated in place like PersistentStack, never hard-deleted.

    `account_id` / `instance_id` identify which TenantBurstAccount and
    which cloud instance to route teardown through
    (services.burst_reconciler / services.burst_provider) — nullable so a
    session can still be metered even if the account row was later
    soft-deleted.
    """
    __tablename__ = "burst_sessions"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(ForeignKey("tenant_burst_accounts.id"), nullable=True, index=True)
    # The cloud instance id/label — matches the `mod-burst-instance` tag
    # burst_bootstrap/burst_reconciler use, and/or the enrolled worker_name.
    instance_id = Column(String(255), nullable=True, index=True)
    worker_name = Column(String(255), nullable=True)
    # GPU class key into burst_pricing.burst_fee_credits_per_hour (e.g.
    # 't4', 'a100'); '' / unknown falls back to 'default' at lookup time.
    gpu_class = Column(String(50), nullable=False, default="default", server_default="default")
    # 'active' | 'draining' | 'closed'
    status = Column(String(20), nullable=False, default="active", server_default="active", index=True)
    joined_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    left_at = Column(DateTime(timezone=True), nullable=True)
    # Idempotency watermark: the tick charges elapsed = now - last_debited_at,
    # then advances this BEFORE the debit commits (stack_billing anchor
    # pattern) so credits + watermark move together or not at all.
    last_debited_at = Column(DateTime(timezone=True), nullable=True)
    credits_debited_total = Column(BigInteger, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)


# =============================================================================
# GX.R4 (launch-completion-megaplan Group X) — Ephemeral run inputs
# =============================================================================
# Owner decision OQ-5(a): TTL 24h -> 7d grace -> BYTES purged in ALL modes
# (even ISO); the RECORD is kept forever (status/timestamps advance, the row is
# never hard-deleted — ISO 27001). Audit events on create/expire/purge.
#
# A row backs a HIDDEN, root-level Cog (``cog_id``) + Version so a run's input
# node consumes the file through the unchanged cog pipeline (COG_IDS ->
# resolve-cog-hash -> content-hash staging). The hidden Cog is filtered out of
# the library listing, so the input is invisible to the user's asset browser.
class AppEphemeralInput(SoftDeleteMixin, Base):
    """A short-TTL file a run can feed an input node without it landing in the
    user's library. ``app_id`` is NULLABLE (no FK yet — GX.A2 adds the FK to
    ``apps`` once that object exists); R4 user-scoped inputs are ``app_id NULL``
    + ``owner_user_id`` scoped.
    """
    __tablename__ = "app_ephemeral_inputs"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    # GX.A2 will add: ForeignKey("apps.id"). Kept as a plain nullable integer
    # until the `apps` table exists so this migration stays self-contained.
    app_id = Column(Integer, nullable=True, index=True)
    owner_user_id = Column(ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    file_extension = Column(String(20), nullable=True)
    mime_type = Column(String(255), nullable=True)
    size_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    # Content-addressed dedup key == ContentBlob.id for the tenant's bytes.
    content_hash = Column(String(64), nullable=False, index=True)
    storage_path = Column(String(1024), nullable=True)
    # Backing (hidden) Cog the run's input node consumes via COG_IDS.
    cog_id = Column(ForeignKey("cogs.id"), nullable=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    # active | expired
    status = Column(String(16), nullable=False, default="active", server_default="active")
    expired_at = Column(DateTime(timezone=True), nullable=True)
    # Set when stage-2 purges the object bytes (ALL modes, OQ-5(a)). The row
    # itself is NEVER deleted.
    bytes_purged_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    tenant = relationship("Tenant")
    owner = relationship("User", foreign_keys=[owner_user_id])
    cog = relationship("Cog", foreign_keys=[cog_id])


# =============================================================================
# GX.S4 (launch-completion-megaplan Group X) — Email send API message record
# =============================================================================
# One row per POST /v1/email/send. METADATA ONLY: recipient lists, total
# size, attachment filename/mime/size — NEVER attachment bytes or message
# bodies (rows must stay small and auditable). Status lifecycle:
# queued -> sent | failed. Bounce/delivery updates (Paid — the
# `usage_metering` entitlement seam in services.email_send_api) arrive from
# the existing Postmark webhook matched on `provider_message_id`.
#
# `app_id` is a plain nullable integer — NO FK yet. The `apps` table was created
# by the shipped migration 20261001_apps_scoped_keys.py (CREATE TABLE IF NOT EXISTS apps);
# these columns stay plain nullable integers with NO FK (the FK was deliberately not added).
class EmailMessage(SoftDeleteMixin, Base):
    """A general email an app sends via the tenant's SMTP (GX.S4)."""
    __tablename__ = "email_messages"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    # GX.A2 will add: ForeignKey("apps.id"). Plain nullable integer until the
    # `apps` table exists so the migration stays self-contained.
    app_id = Column(Integer, nullable=True, index=True)
    sender_user_id = Column(ForeignKey("users.id"), nullable=True, index=True)
    to = Column(JSONB, nullable=False, default=list, server_default="[]")
    cc = Column(JSONB, nullable=False, default=list, server_default="[]")
    bcc = Column(JSONB, nullable=False, default=list, server_default="[]")
    subject = Column(String(512), nullable=False)
    size_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    # [{filename, mime_type, size_bytes}, ...] — metadata only, no bytes.
    attachments = Column(JSONB, nullable=True, default=list)
    # queued | sent | failed
    status = Column(String(16), nullable=False, default="queued", server_default="queued")
    error = Column(Text, nullable=True)
    provider_message_id = Column(String(512), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    tenant = relationship("Tenant")
    sender = relationship("User", foreign_keys=[sender_user_id])


class TenantLlmCall(SoftDeleteMixin, Base):
    """One row per /v1/llm/chat/completions call (GX.S5).

    Metering record — METADATA ONLY. It stores which model the call used,
    the token counts the ENGINE reported (from the OpenAI `usage` block,
    never an estimate), the wall-clock latency and the outcome status.
    It NEVER stores the prompt or the completion text (ISO 27001 hygiene,
    same posture as `EmailMessage`).

    Named `TenantLlmCall` (not `LlmUsage`) because the idle-reclamation
    `LlmUsage` table already owns that Python name — reusing it would
    shadow `record_usage`'s target and silently break idle reclaim.

    `app_id` is a plain nullable integer — NO FK yet. The `apps` table
    was created by the shipped migration 20261001_apps_scoped_keys.py (CREATE TABLE IF NOT EXISTS apps);
    these columns stay plain nullable integers with NO FK (the FK was deliberately not added).
    """
    __tablename__ = "tenant_llm_calls"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    # Plain nullable integer until the `apps` table exists (no FK yet).
    app_id = Column(Integer, nullable=True, index=True)
    user_id = Column(ForeignKey("users.id"), nullable=True, index=True)
    # The model name the call was made against (tenant-visible stack model).
    model = Column(String(255), nullable=False)
    # Token counts come from the engine's `usage` block — never estimated.
    prompt_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    completion_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    # Wall-clock round-trip to the backend, in ms.
    latency_ms = Column(Integer, nullable=True)
    # "success" | "failed" (a failed call still consumed input tokens).
    status = Column(String(16), nullable=False, default="success",
                    server_default="success")
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)

    # Relationships
    tenant = relationship("Tenant")
    user = relationship("User", foreign_keys=[user_id])


# =============================================================================
# GX.S6 (launch-completion-megaplan Group X) — per-stack/app storage scopes
# =============================================================================
# Tombstone/audit + retention-hold record for objects under a scope prefix
# (``stacks/{stack_id}/`` or ``apps/{app_id}/`` in the tenant's file bucket
# or BYO-S3 bucket). SOFT-DELETE ONLY (ISO 27001 — no hard deletes): a
# delete flips status to 'deleted' + deleted_at and moves the BYTES to the
# scope's ``.trash/`` prefix; the row is never removed.
# ``retention_hold_until`` carries the Paid-only retention/compliance hold
# (usage_metering capability seam); CE leaves it NULL.
class StorageScopeObject(Base):
    """Tombstone + retention record for one object in a storage scope."""
    __tablename__ = "storage_scope_objects"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    # 'stack' | 'app'
    scope_kind = Column(String(16), nullable=False, index=True)
    # No FK: scope_id is a persistent_stacks.id for stacks; for apps it is a
    # plain integer until the `apps` table exists (GX.A3 adds the FK).
    scope_id = Column(BigInteger, nullable=False, index=True)
    object_key = Column(String(512), nullable=False, index=True)
    # 'live' | 'deleted'
    status = Column(String(16), nullable=False, default="live",
                    server_default="live")
    size_bytes = Column(BigInteger, nullable=True)
    content_type = Column(String(255), nullable=True)
    # Paid-only (usage_metering): blocks soft-delete until expiry.
    retention_hold_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    __table_args__ = (
        UniqueConstraint("tenant_id", "scope_kind", "scope_id", "object_key",
                         name="uq_storage_scope_objects_key"),
    )


# =============================================================================
# GX.S7 (launch-completion-megaplan Group X) — scheduled HTTP triggers
# =============================================================================
# A tenant/app schedules signed HTTP calls against a Persistent Stack service
# or an App target over the internal network. cron (5-field) OR interval
# seconds. Every call is HMAC-SHA256 signed with a per-trigger secret
# (encrypted under the tenant key, ApiKey.encrypted_keycloak_secret pattern,
# returned ONCE at creation). Each fire writes an append-only
# `HttpTriggerRun` history row. On failure (non-2xx / timeout / N consecutive)
# an alert fans out to MOD notification + email + outbound webhook.
#
# SOFT-DELETE ONLY (ISO 27001). The `http_trigger_runs` table is APPEND-ONLY:
# a BEFORE DELETE trigger (in the migration) raises on any physical delete,
# so history rows are immutable. `app_id` is a plain integer until the `apps`
# table (GX.A2) exists — same pattern as EmailMessage.
class HttpTrigger(SoftDeleteMixin, Base):
    """One scheduled, signed HTTP trigger (GX.S7)."""
    __tablename__ = "http_triggers"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    # GX.A2 adds: ForeignKey("apps.id"). Plain nullable integer until the
    # `apps` table exists so the migration stays self-contained.
    app_id = Column(Integer, nullable=True, index=True)
    name = Column(String(255), nullable=False)
    # 'cron' (uses cron_expression) | 'interval' (uses interval_seconds)
    schedule_kind = Column(String(16), nullable=False)
    cron_expression = Column(String(128), nullable=True)
    interval_seconds = Column(Integer, nullable=True)
    # HTTP method for the call (default POST).
    method = Column(String(8), nullable=False, default="POST", server_default="POST")
    # Path on the target (leading '/'); empty = root.
    path = Column(String(512), nullable=False, default="", server_default="")
    # Target identity — which internal service this calls. The concrete URL
    # is RESOLVED from these rows at fire time (never stored freehand).
    # 'stack-service' (stack_id + stack_service + stack_port) | 'app' (app_id).
    target_kind = Column(String(16), nullable=False)
    stack_id = Column(ForeignKey("persistent_stacks.id"), nullable=True, index=True)
    stack_service = Column(String(127), nullable=True)
    stack_port = Column(Integer, nullable=True)
    # HMAC-SHA256 per-trigger secret, encrypted under the tenant key
    # (ApiKey.encrypted_keycloak_secret pattern). Returned ONCE at creation;
    # never readable afterwards.
    encrypted_secret = Column(Text, nullable=False)
    # Optional outbound-webhook target for the failure alert fan-out
    # (third channel, alongside MOD notification + email). Signed with the
    # same per-trigger secret (X-MOD-Signature / X-MOD-Timestamp).
    callback_url = Column(String(1024), nullable=True)
    # active | paused
    status = Column(String(16), nullable=False, default="active", server_default="active")
    paused_at = Column(DateTime(timezone=True), nullable=True)
    paused_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    # Rolling consecutive-failure count; reset to 0 on a success.
    consecutive_failures = Column(Integer, nullable=False, default=0, server_default="0")
    last_fired_at = Column(DateTime(timezone=True), nullable=True)
    next_fire_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_alerted_at = Column(DateTime(timezone=True), nullable=True)
    created_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    tenant = relationship("Tenant")


class HttpTriggerRun(Base):
    """APPEND-ONLY run history for one fire of an HttpTrigger (GX.S7).

    NEVER updated or deleted — a BEFORE DELETE trigger (migration
    20261002_http_triggers) raises on any physical delete. ISO 27001: the
    run history is an immutable audit record of every call the trigger made.
    """
    __tablename__ = "http_trigger_runs"
    id = Column(Integer, primary_key=True)
    trigger_id = Column(ForeignKey("http_triggers.id"), nullable=False, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    # scheduled | success | failed | skipped
    status = Column(String(16), nullable=False)
    resolved_url = Column(String(1024), nullable=True)
    http_status = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    # True when this failure tripped the consecutive-failure alert (the
    # three-channel fan-out was attempted).
    alert_sent = Column(Boolean, nullable=False, default=False, server_default="false")
    fired_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Relationships
    trigger = relationship("HttpTrigger")
    tenant = relationship("Tenant")


# =============================================================================
# GT.D8.2 (launch-completion-megaplan) — tenant Kubernetes cluster registry
# =============================================================================
# A row per tenant Kubernetes cluster the k8s backend may dispatch to.
# `kubeconfig_vault_ref` holds ONLY the OpenBao path of the kubeconfig secret —
# never the kubeconfig itself. `in_cluster` clusters use the API's
# ServiceAccount instead (no ref). SOFT-DELETE ONLY (ISO 27001):
# deleted_at tombstones the row, it is never physically deleted.
class K8sClusterConfig(SoftDeleteMixin, Base):
    """One Kubernetes cluster a tenant may run k8s-backend workloads on."""
    __tablename__ = "k8s_cluster_configs"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    # OpenBao path (e.g. 'secret/mod/k8s/<name>') of the kubeconfig secret.
    # NEVER the kubeconfig content itself. NULL when in_cluster.
    kubeconfig_vault_ref = Column(String(512), nullable=True)
    # True = use the API process's in-cluster ServiceAccount (no ref).
    in_cluster = Column(Boolean, nullable=False, default=False, server_default="false")
    # Only active clusters are dispatched to / counted against the tier.
    active = Column(Boolean, nullable=False, default=True, server_default="true")
    # 18.2 (GI.2) capability probe output (GFD node labels, KAI, MIG) as a
    # JSONB dict — the exact shape services.k8s_gpu_request.cluster_caps
    # consumes (kai/kai_queue/mig_profiles/time_slicing_resource/vendor/
    # gpu_memory_gb + diagnostics). NULL until first probe.
    cluster_caps = Column(JSONB, nullable=True)
    # When the probe last succeeded (UTC); NULL = never probed.
    caps_probed_at = Column(DateTime(timezone=True), nullable=True)
    # 18.3 (GI.3) admin override: explicit tenant/project -> EXISTING KAI
    # queue name map {"tenant:<id>": "name", "tenant:<id>:project:<slug>":
    # "name"}. Mapped queues are only VALIDATED read-only, never created
    # or mutated (services.k8s_kai_queues). NULL = deterministic MOD
    # queues (mod-t<tenant_id>) only.
    kai_queue_map = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)

    __table_args__ = (
        # A soft-deleted cluster does not squat its name: a tenant can
        # delete "prod" and immediately recreate "prod" (the tombstone
        # hangs around for the retention period, ISO 27001).
        Index(
            "uq_k8s_cluster_configs_live_tenant_name",
            "tenant_id", "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    # Relationships
    tenant = relationship("Tenant")


class ConformanceCheckResult(Base):
    """GY.C12 — one conformance-check result row (append-only).

    `tenant_id` NULL = platform-scoped result (the engine reports the
    platform's own posture, not a tenant's). `run_id` groups the rows
    of one run_checks pass; `controls` is the check's per-framework
    control-id mapping at run time (the contract every other task plugs
    into); `evidence` is metadata-only (setting values/booleans, never
    content). Append-only by DB trigger (ISO 27001: no hard deletes —
    migration 20261003f_conformance_check_results.py), like the audit
    tables.
    """
    __tablename__ = "conformance_check_results"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, nullable=True, index=True)  # NULL = platform
    check_id = Column(String(64), nullable=False, index=True)
    status = Column(String(16), nullable=False, index=True)  # pass|fail|manual|error
    evidence = Column(JSONB, nullable=True)
    controls = Column(JSONB, nullable=True)
    trigger = Column(String(16), nullable=False)  # schedule|on_demand
    run_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)

    __table_args__ = (
        Index("ix_conformance_check_results_check_created",
              "check_id", "created_at"),
    )


class ErasureLedger(Base):
    """Q3(c) — FX.15 restore-safety: an append-only ledger of privacy
    erasures that SURVIVES a restore of the main DB.

    Problem it fixes: a backup taken before a user/tenant was erased, when
    restored, brings the erased person's PII back (the DB rows the erasure
    scrubbed come back from the dump). The erasure itself only lives in the
    restored database (``users.erased_at`` / ``tenants.bytes_purged_at`` /
    the scrubbed fields), so it is exactly the data a pre-erasure backup
    overwrites.

    Fix: every completed erasure appends ONE row here (ID-only, NO PII).
    This table is mirrored out-of-DB as an append-only JSONL file
    (see ``services/erasure_ledger.py``); that file lives outside the
    PostgreSQL volume so a restore cannot clobber it. After any restore,
    ``scripts/replay_erasures.py`` reads the ledger and re-applies each
    recorded erasure, so a restored DB can never resurrect PII.

    Columns are identifiers + timestamps only — never email, name, bucket
    contents or other personal data (GDPR / ISO 27001 A.9.2). Append-only:
    a DB trigger (migration 20261003i_erasure_ledger.py) refuses
    UPDATE/DELETE/TRUNCATE, mirroring the audit tables.

    ``request_id`` keys the DB row to its mirror line in the JSONL file
    (one row == one line, same id). It is a UUID generated at erasure time;
    it is NOT a PII value and is safe to persist on both sides.
    """
    __tablename__ = "erasure_ledger"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # UUID linking this row to its mirror line in the JSONL ledger file.
    request_id = Column(String(36), nullable=False, unique=True, index=True)
    # Which kind of subject was erased: "user" | "tenant".
    subject_type = Column(String(16), nullable=False)
    # The subject's row id (users.id / tenants.id). Identifier only.
    subject_id = Column(Integer, nullable=False, index=True)
    # Which tenant the erasure pertains to (NULL for a tenant erasure, since
    # the subject IS the tenant). Kept for audit attribution.
    tenant_id = Column(Integer, nullable=True, index=True)
    # The moment the erasure finished (mirrors users.erased_at /
    # tenants.bytes_purged_at at write time).
    erased_at = Column(DateTime(timezone=True), nullable=False)
    # Narrower scope of what was destroyed, for audit: e.g. "user_pii_and_bytes"
    # or "tenant_bytes_and_key". No PII, descriptive only.
    scope = Column(String(64), nullable=True)
    # Actor user id that triggered the erasure (the daily sweep writes NULL;
    # an operator-initiated erasure records itself). Identifier only.
    actor_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)

    __table_args__ = (
        Index("ix_erasure_ledger_subject", "subject_type", "subject_id"),
    )


class ConsentRecord(Base):
    """GY.V5 — verifiable consent records (COPPA parental consent today).

    An APPEND-ONLY store of consent grants/revocations per data subject.
    ``consent_type`` is an extensible taxonomy — ``parental_coppa`` is the
    first; a later BIPA task (GY.V8) will add ``biometric`` without a
    schema change (see ``services/consent.py::CONSENT_TYPES``).

    ISO 27001 / COPPA: the consent HISTORY must be reconstructable, so
    revocation is a NEW row with ``status='revoked'`` (and
    ``revoked_record_id`` pointing at the grant it revokes), never an
    UPDATE or DELETE. A DB trigger (migration
    20261004c_consent_records.py) refuses UPDATE/DELETE/TRUNCATE,
    mirroring the audit tables and the erasure ledger.

    ``guardian_contact`` is PII: the service layer (services/consent.py)
    encrypts it at rest with the tenant-key Fernet pattern
    (``encrypt_secret``/``decrypt_secret`` in utils/http_triggers.py)
    before insert and decrypts only on an explicit admin read.
    """
    __tablename__ = "consent_records"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False,
                       index=True)
    # The data subject this consent covers (the minor / the biometric
    # subject). Identifier only — PII lives on the users row.
    subject_user_id = Column(Integer, nullable=False, index=True)
    # Extensible taxonomy: 'parental_coppa' (today), 'biometric' (GY.V8).
    consent_type = Column(String(32), nullable=False,
                          default='parental_coppa')
    # Guardian contact, ENCRYPTED at rest (see class docstring). NULL when
    # the method does not capture a contact (e.g. card verification).
    guardian_contact = Column(Text, nullable=True)
    # How the (parental) consent was verified.
    method = Column(String(32), nullable=False, default='other')
    # 'granted' | 'revoked'. A revocation is a new row, not a flip.
    status = Column(String(16), nullable=False, default='granted',
                    index=True)
    # The admin (users.id) who recorded the entry.
    recorded_by = Column(Integer, nullable=True)
    # Reference to the evidence (document id / email id / form upload).
    evidence_ref = Column(String(512), nullable=True)
    # On a 'revoked' row: the id of the granted record being revoked.
    revoked_record_id = Column(Integer, nullable=True)
    # GY.V8 (BIPA 740 ILCS 14) — the retention deadline stated in the
    # written release (see evidence_ref) for a 'biometric' grant. NULL =
    # no explicit retention stated; the destruction sweep then falls
    # back to 3 years from the subject's last interaction.
    biometric_retention_until = Column(DateTime(timezone=True),
                                       nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)

    __table_args__ = (
        Index("ix_consent_records_subject", "subject_user_id", "consent_type"),
    )


# =============================================================================
# GY.C15b — incident export job record (its own table)
# =============================================================================

class IncidentExportJobStatus(str, enum.Enum):
    """Lifecycle of an incident export package build."""
    PENDING = "pending"
    BUILDING = "building"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"


class IncidentExportJob(Base):
    """
    GY.C15b — one requested incident export package.

    The requested window is persisted here (it used to be reconstructed as
    `created_at -> now`, so a job built a different window than the one the
    admin asked for), and the completed package's manifest sha256 has its own
    column instead of being stuffed into `error_message`.

    ISO 27001: no hard deletes — `deleted_at` is the soft-delete marker.
    `tenant_id` is nullable: a platform-wide export has no tenant.
    """
    __tablename__ = "incident_export_jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True,
                        index=True)
    requested_by = Column(Integer, index=True)  # users.id (no FK: audit rows
                                                # can be recorded for user 0)
    # The window the admin requested, tz-aware.
    window_start = Column(DateTime(timezone=True), nullable=False)
    window_end = Column(DateTime(timezone=True), nullable=False)

    # native_enum=False keeps the column text (matches the applied migration,
    # which created `status TEXT`), while the Python side stays an enum.
    status = Column(Enum(IncidentExportJobStatus, native_enum=False, length=32),
                    default=IncidentExportJobStatus.PENDING, nullable=False,
                    index=True)
    object_key = Column(String, nullable=True)      # exports/incidents/<id>/package.zip
    manifest_sha256 = Column(String, nullable=True)  # sha256 of manifest.json
    error_message = Column(String, nullable=True)    # failure text only

    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # ISO 27001: soft delete only.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_incident_export_jobs_tenant_window", "tenant_id", "window_start"),
    )


# =============================================================================
# GSEC FX.59 part 3 (owner decision Q42=A) — self-serve tenant export job
# =============================================================================

class TenantExportJobStatus(str, enum.Enum):
    """Lifecycle of a self-serve tenant export package build."""
    PENDING = "pending"
    BUILDING = "building"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"


class TenantExportJob(Base):
    """
    GSEC FX.59 part 3 — one self-serve full tenant export package.

    Built during the contract-end export window (read_only state and for
    `tenant_lifecycle.export_window_days` after contract end): workflows,
    files, outputs and the tenant's audit log, zipped, uploaded to the
    tenant's own bucket under `exports/`, and handed back as a presigned
    download link with a short expiry.

    Job state follows the IncidentExportJob pattern (own table, status
    enum stored as text). ISO 27001: no hard deletes — `deleted_at` is the
    soft-delete marker; expired jobs are soft-deleted, never removed.
    """
    __tablename__ = "tenant_export_jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False,
                       index=True)
    requested_by = Column(Integer, index=True)  # users.id (no FK: audit rows
                                                # can be recorded for user 0)

    # native_enum=False keeps the column text (matches the applied migration,
    # which created `status TEXT`), while the Python side stays an enum.
    status = Column(Enum(TenantExportJobStatus, native_enum=False, length=32),
                    default=TenantExportJobStatus.PENDING, nullable=False,
                    index=True)
    object_key = Column(String, nullable=True)   # exports/<id>/package.zip
    size_bytes = Column(BigInteger, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(String, nullable=True)         # failure text only

    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # ISO 27001: soft delete only.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_tenant_export_jobs_tenant_created", "tenant_id", "created_at"),
    )


# =============================================================================
# GSEC FX.77 part 1a — tenant log-stream (SIEM export) config
# (services/log_streams.py). Vault secret refs: p1b. Router: p1c.
# Senders: p2 (syslog/TLS), p3 (HTTPS/Splunk HEC), p4 (scheduled S3 export).
# =============================================================================

class TenantLogStream(Base):
    """
    GSEC FX.77 part 1a — one tenant-configured SIEM export destination.

    Non-secret config only: endpoint/kind/options live here; the actual
    secret (TLS client key, webhook bearer token, HEC token, S3 secret key)
    is never stored in this table — only a Vault path reference
    (`secret_ref`, wired in part 1b). ISO 27001: no hard deletes —
    `deleted_at` is the soft-delete marker.
    """
    __tablename__ = "tenant_log_streams"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # FX.77 s2 (owner-approved existing-column change): NULL tenant_id = the
    # ONE platform-level (tenant-less) log stream for platform/system events.
    # Tenant code paths filter on `tenant_id == <id>` and therefore never see
    # it; the DB keeps at most one LIVE such row (partial unique index,
    # migrations/20261008a_fx77_platform_log_streams.py).
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True,
                       index=True)
    name = Column(String(120), nullable=False)
    kind = Column(String(32), nullable=False)  # syslog_tls|https_webhook|
                                                # splunk_hec|s3_export
    endpoint = Column(String(512), nullable=False)
    # Non-secret options only: facility, format, batch size, S3 prefix, etc.
    config = Column(PostgresqlJSONB, nullable=False, server_default="{}")
    # Vault path reference; the secret value itself is never stored here.
    secret_ref = Column(String(256), nullable=True)
    enabled = Column(Boolean, nullable=False, default=False,
                     server_default="false")
    # Subset of ("audit", "security", "run", "auth").
    event_types = Column(PostgresqlJSONB, nullable=False, server_default="[]")
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_error_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(500), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)
    # ISO 27001: soft delete only.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_tenant_log_streams_tenant_name"),
    )


# =============================================================================
# GSEC FX.50 part 2 (owner decision Q33=A) — breach-notification incident
# tracker (services/security_incidents.py)
# =============================================================================

class SecurityIncidentStatus(str, enum.Enum):
    """Lifecycle of a tracked security/breach incident.

    Q33=A: affected customers are told within 72 hours of CONFIRMING a
    breach; tenants on a gov/DoD compliance profile get an initial
    heads-up within 24 hours. ``NOTIFIED`` = the affected-tenant
    notification has gone out; the deadline clocks (``notify_due_at`` /
    ``gov_headsup_due_at``) are stamped at confirm time."""
    OPEN = "open"
    CONFIRMED = "confirmed"
    NOTIFIED = "notified"
    CLOSED = "closed"


class SecurityIncident(Base):
    """
    GSEC FX.50 part 2 — one tracked security/breach incident.

    ``tenant_id`` is nullable: a platform-wide incident (e.g. a shared
    worker compromise) has no tenant. The two deadline clocks are
    computed at confirm time from the ``incident.*`` platform settings:

      * ``notify_due_at``      = confirmed_at + 72h (affected-customer
        notification — Q33=A "within 72 hours")
      * ``gov_headsup_due_at`` = confirmed_at + 24h, ONLY on installs
        with an active gov/DoD compliance profile (cmmc /
        fedramp_high_il5); NULL otherwise (no 24h clock to miss)

    ``notified_at`` stamps when the affected tenant was actually told;
    ``services.security_incidents.overdue()`` reports incidents whose
    clock has passed with ``notified_at`` still NULL.

    ISO 27001: no hard deletes — ``deleted_at`` is the soft-delete
    marker; follow-ups live in ``IncidentFollowUp`` (also soft-delete
    only).
    """
    __tablename__ = "security_incidents"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True,
                       index=True)
    severity = Column(String(16), nullable=False, index=True)
    summary = Column(String(500), nullable=True)
    status = Column(Enum(SecurityIncidentStatus, native_enum=False, length=32),
                    default=SecurityIncidentStatus.OPEN, nullable=False,
                    index=True)
    detected_at = Column(DateTime(timezone=True), nullable=False)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    gov_headsup_due_at = Column(DateTime(timezone=True), nullable=True)
    notify_due_at = Column(DateTime(timezone=True), nullable=True)
    notified_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(Integer, nullable=True)
    confirmed_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, onupdate=func.now())
    # ISO 27001: soft delete only.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    follow_ups = relationship("IncidentFollowUp", back_populates="incident",
                              cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="ck_security_incidents_severity"),
        # The Enum column persists member NAMES (SQLAlchemy default —
        # same as IncidentExportJob.status), so the CHECK names them.
        CheckConstraint(
            "status IN ('OPEN', 'CONFIRMED', 'NOTIFIED', 'CLOSED')",
            name="ck_security_incidents_status"),
        Index("ix_security_incidents_tenant_detected",
              "tenant_id", "detected_at"),
        Index("ix_security_incidents_notify_due", "notify_due_at"),
    )


class IncidentFollowUp(Base):
    """
    GSEC FX.50 part 2 — one follow-up action on a security incident
    (Q33=A: "follow-ups as facts firm up"). ``owner`` is free-form
    (a named platform admin / team); ``due_at`` is the working deadline
    and ``done_at`` stamps completion (NULL = still open). Soft-delete
    only (ISO 27001 — ``deleted_at``; no hard deletes anywhere).
    """
    __tablename__ = "incident_follow_ups"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    incident_id = Column(String, ForeignKey("security_incidents.id"),
                         nullable=False, index=True)
    owner = Column(String(255), nullable=False)
    note = Column(Text, nullable=True)
    due_at = Column(DateTime(timezone=True), nullable=False)
    done_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, onupdate=func.now())
    # ISO 27001: soft delete only.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    incident = relationship("SecurityIncident", back_populates="follow_ups")


class EmailConsentEvent(Base):
    """FX.140 part 2 (owner decision Q128=A) — email-consent record.

    APPEND-ONLY (ISO 27001: no hard deletes, and no updates either): every
    consent decision is a new row, never an edit of an old one. The CURRENT
    consent for a (user, category) pair is the LATEST event for that pair
    (`created_at` desc, `id` desc as the tie-breaker). History is therefore
    free — the row that granted consent at sign-up and the row that revoked
    it via a one-click link both stay, which is what a compliance audit of
    "did we ever send marketing email to a user who had opted out" needs.

    `granted` is the decision: True = consented (may receive this category),
    False = opted out. `source` records HOW the decision was made —
    signup | preferences | unsubscribe_link | admin — so a one-click link
    click is distinguishable from a Dashboard toggle. `wording_version` is
    the consent-text version shown at the time of the decision (a
    consent-with-correct-records requirement: the wording that justified
    the decision is recoverable, not just the boolean).

    Categories are free-form lowercase strings (the same vocabulary
    services/email_unsubscribe uses: product, marketing, security,
    billing, ...). Service/transactional categories are recorded too but
    are NEVER suppressed by consent (Q128=A) — the service layer enforces
    that, not the table.
    """
    __tablename__ = "email_consent_events"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    tenant_id = Column(Integer, nullable=True, index=True)
    category = Column(String(48), nullable=False, index=True)
    granted = Column(Boolean, nullable=False)
    source = Column(String(32), nullable=False)
    wording_version = Column(String(32), nullable=False,
                             server_default="email-consent-v1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)

    __table_args__ = (
        Index("ix_email_consent_events_user_category", "user_id", "category"),
    )


class AbuseReport(SoftDeleteMixin, Base):
    """FX.93 part 1 (owner decision Q79=A) — abuse reporting backend.

    One row per abuse report submitted by a (normally authenticated)
    user about a workflow, node, file, output, account or other target.
    Platform admins triage from the admin queue; ``triage_due_at`` is
    the SLA clock — created + 1 BUSINESS day (weekends skipped; no
    holiday calendar), computed at creation (services/abuse_reports.py).

    Status lifecycle: open -> triaged -> actioned | dismissed. A
    target-side ``appealed`` row is recorded when the affected party
    appeals a decision. ``suspend_target`` triage does NOT suspend
    anything itself — it only RECORDS the requested suspension plus an
    audit event; executing the suspension is a later part (Planned).

    SOFT-DELETE ONLY (ISO 27001 — no hard deletes): removal sets
    ``deleted_at``; the row stays for the compliance record. Every
    state change writes a PermissionAuditLog row via the existing
    audit helpers (ABUSE_REPORT_CREATED / ABUSE_REPORT_TRIAGED); this
    table holds no audit trail of its own.
    """
    __tablename__ = "abuse_reports"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The tenant whose content/account is being reported (NULL when the
    # reporter doesn't know / it is a platform-level target).
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True,
                       index=True)
    # Reporter identity (NULL for anonymous-ish reports where only the
    # contact email is known).
    reporter_user_id = Column(Integer, ForeignKey("users.id"), nullable=True,
                              index=True)
    reporter_email = Column(String(255), nullable=True)
    # 'workflow' | 'node' | 'file' | 'output' | 'account' | 'other'.
    target_type = Column(String(24), nullable=False, index=True)
    # String id of the target (workflow id, file key, account email, ...).
    target_id = Column(String(255), nullable=False)
    # 'csam' | 'violent' | 'harassment' | 'ip' | 'spam' | 'malware' |
    # 'other'.
    category = Column(String(24), nullable=False, index=True)
    description = Column(Text, nullable=True)
    # 'open' | 'triaged' | 'actioned' | 'dismissed' | 'appealed'.
    status = Column(String(16), nullable=False, default='open', index=True)
    # SLA clock: created + 1 business day (weekends skipped).
    triage_due_at = Column(DateTime(timezone=True), nullable=False, index=True)
    resolution_note = Column(Text, nullable=True)
    # The platform admin (users.id) who last acted on the report.
    resolved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    # FX.93 p2a — the platform admin (users.id) CLAIMED for triage
    # (Airflow-style "assign this report to me"); NULL = unassigned.
    # p2c owns the transitions that set/clear it; this part only ships
    # the column + the enum so the queue page and later parts can see it.
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True,
                         index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, onupdate=func.now())

    __table_args__ = (
        Index("ix_abuse_reports_status_due", "status", "triage_due_at"),
        Index("ix_abuse_reports_assigned_due", "assigned_to",
              "triage_due_at"),
    )


class LikenessConsent(SoftDeleteMixin, Base):
    """FX.98 part 1 (owner decision Q84=A) — likeness consent register.

    A record that a REAL person's likeness (face/voice, on a node tagged
    ``likeness``) may be replicated by this tenant. Running a likeness-tagged
    node on a real person requires a VALID, IN-SCOPE consent record linked
    from the run params (services/likeness_consent.py::check_run_allowed);
    an expired / revoked / out-of-scope record blocks the run. When the
    subject is synthetic (no real person), a synthetic attestation is
    recorded instead (record_synthetic_attestation) and no consent is
    required.

    SOFT-DELETE ONLY (ISO 27001 — no hard deletes): ``revoke`` sets
    ``revoked_at``; a soft-delete (the register's admin "delete") sets
    ``deleted_at`` via the mixin. The row always stays for the compliance
    record. Every state change writes a PermissionAuditLog row via
    audit.log_security_event (LIKENESS_CONSENT_CREATED /
    LIKENESS_CONSENT_REVOKED / LIKENESS_CONSENT_DELETED).
    """
    __tablename__ = "likeness_consents"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The tenant that owns / uses this consent record.
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False,
                       index=True)
    # Display name of the real person whose likeness is covered. This is
    # PII — the register is tenant-admin only and reads never fan out to
    # end users; the run-gate audit records only the consent id, never the
    # name.
    person_name = Column(String(255), nullable=False)
    # Free-text scope of what this consent covers (e.g. "marketing voice
    # clone", "character face in film 'X'"). The run-gate checks the
    # requested use_scope against this (see check_run_allowed).
    scope = Column(Text, nullable=True)
    # Consent expiry — a consent past this timestamp is refused. NULL means
    # no expiry (consent is open-ended until revoked).
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Optional link to an uploaded signed consent document (files.id).
    consent_doc_file_id = Column(Integer, nullable=True)
    # Optional external contract reference (e.g. a contract number) as an
    # alternative to an uploaded document.
    contract_ref = Column(String(255), nullable=True)
    # The tenant-admin (users.id) who recorded this consent.
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    # Set when the consent is revoked (a soft, reversible-in-audit sense
    # state: revocation blocks the run; the row is never removed).
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # SoftDeleteMixin adds `deleted_at` (nullable, indexed).

    __table_args__ = (
        Index("ix_likeness_consents_tenant_expiry", "tenant_id", "expires_at"),
    )


class StaffRecord(SoftDeleteMixin, Base):
    """GSEC FX.153 part 2 (owner decision Q140=B) — in-app personnel
    security-policy staff register (docs/isms/21-personnel-security-policy.md
    "In-app records"; services/staff_register.py).

    One row per person on the MOD payroll/security boundary: name, email,
    role, staff_type (employee | contractor), the NDA/AUP signed dates, the
    training-completed date, the gov-cleared + US-person flags, and the
    leaver flow: ``leaver_at`` starts the departure, ``leaver_checklist``
    (JSONB) maps the policy's 7 systems (keycloak, vault, harbor, git,
    tailnet, cloud_accounts, colo_access_list) to a ``removed_at`` per
    system as each is closed.

    SOFT-DELETE ONLY (ISO 27001 — no hard deletes): the register's delete
    sets ``deleted_at`` via the mixin; the row always stays for the
    compliance record (policy "Records retention": staff-register rows
    kept >= 3 years). Contractors get NO automatic expiry — the leaver
    flow is owner-initiated (POST /{id}/leaver). Every write is audited
    (STAFF_RECORD_* / STAFF_LEAVER_* AuditAction values).
    """
    __tablename__ = "staff_records"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The platform tenant this person works for (0 = system/platform-level
    # staff). FK-less like other platform-admin registers: platform staff
    # may not belong to a tenant row.
    tenant_id = Column(Integer, nullable=True, index=True)
    person_name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True)
    role = Column(String(255), nullable=True)
    staff_type = Column(String(32), nullable=False, index=True,
                        default="employee")  # employee | contractor
    # Personnel-policy sign-offs (ISO/NSA-style documents).
    nda_signed_at = Column(DateTime(timezone=True), nullable=True)
    aup_signed_at = Column(DateTime(timezone=True), nullable=True)
    # When the person's disciplinary training was last completed; NULL =
    # never recorded (training-due). Overdue window:
    # staff.training_interval_days platform setting.
    training_completed_at = Column(DateTime(timezone=True), nullable=True,
                                   index=True)
    gov_cleared = Column(Boolean, nullable=False, default=False,
                         index=True)
    us_person_confirmed = Column(Boolean, nullable=False, default=False)
    # Set when a departure is known (staff leaves / contract ends / role
    # dropped). The leaver checklist must close within
    # staff.leaver_access_removal_hours (default 24).
    leaver_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # JSONB {system: removed_at-iso} for the 7 policy systems; a system
    # absent or null = not yet removed. Written only via the audited
    # PATCH /{id}/leaver-checklist.
    leaver_checklist = Column(JSONB, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)
    # SoftDeleteMixin adds `deleted_at` (nullable, indexed).
    # Indexes come from the column index=True flags only (auto-named
    # ix_staff_records_* — same names as migrations/20261007b): an
    # explicit Index with the same name would collide on create_all.


class BreakGlassCredential(SoftDeleteMixin, Base):
    """GSEC FX.49 part 1 (owner decision Q32=A) — break-glass custody.

    The platform break-glass credential is SPLIT: it lives as N
    custodian shares (the owner decision: one half sealed in Vault
    behind two-person approval, the other half printed in a sealed
    envelope in the owner's physical safe). A use requires BOTH halves
    to be assembled, so the API never holds the plaintext: each share
    is stored wrapped (or, for the printed share, by hash only) and the
    row itself carries no usable secret. Reassembly of the plaintext
    happens outside the DB (Vault + envelope) at rotation time; this
    table is the custody record.

    ``total_shares`` >= 2 is enforced at the service layer. After the
    credential is used, ``rotation_required`` flips True (part 2 turns
    that into the alert + banner + forced-rotation flow);
    ``rekeyed_at`` / ``rekeyed_by_user_id`` record the post-emergency
    rotation that clears it.

    SOFT-DELETE ONLY (ISO 27001 — no hard deletes): removal sets
    ``deleted_at``; the row always stays as the compliance record.
    """
    __tablename__ = "break_glass_credentials"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # Stable display name of the credential (e.g. "platform-admin-2fa");
    # audit metadata carries this, never key material.
    name = Column(String(255), nullable=False, unique=True)
    # Number of custodian shares the credential is split into (>= 2).
    total_shares = Column(Integer, nullable=False, default=2)
    # Version of the split; bumped on rotation. Plaintext halves are
    # never stored — ``shares`` maps share_index -> wrapped share or
    # sha256 hex of the printed share.
    generation = Column(Integer, nullable=False, default=1, index=True)
    shares = Column(JSONB, nullable=False, default=dict)
    # The owner decision: one half sealed in Vault behind two-person
    # approval. Vault logical path the sealed half lives under
    # (identifier only — the secret itself is in Vault, not the DB).
    vault_secret_path = Column(String(512), nullable=True)
    # True until the post-emergency rotation completes (part 2 forces
    # it); a use never self-clears it.
    rotation_required = Column(Boolean, nullable=False, default=False,
                               index=True)
    rotation_required_at = Column(DateTime(timezone=True), nullable=True,
                                  index=True)
    # When/who completed the rotation that clears rotation_required.
    rekeyed_at = Column(DateTime(timezone=True), nullable=True)
    rekeyed_by_user_id = Column(Integer, ForeignKey("users.id"),
                                nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=True)
    # SoftDeleteMixin adds `deleted_at` (nullable, indexed).


class BreakGlassUseRequest(SoftDeleteMixin, Base):
    """GSEC FX.49 part 1 (owner decision Q32=A) — one requested USE of
    a split break-glass credential, gated by two-person approval.

    A platform admin files a request (reason required) against a
    credential; a DIFFERENT platform admin must approve it before it
    can be used. The approval lapses when ``expires_at`` passes
    (swept to ``expired`` by the service's expire helper) — an unused
    approved request never grants anything on its own. Use is
    SINGLE-USE: ``used`` is a terminal state (``used_at`` /
    ``used_by_user_id`` stamped, credential ``rotation_required`` set);
    a second use of the same request is refused.

    Status lifecycle: pending -> approved | denied | expired;
    approved -> used | expired. Every state change writes a
    PermissionAuditLog row via the audit helpers (BREAK_GLASS_REQUESTED
    / BREAK_GLASS_APPROVED / BREAK_GLASS_DENIED / BREAK_GLASS_EXPIRED /
    BREAK_GLASS_USED); metadata carries ids + reason — NEVER a share.

    SOFT-DELETE ONLY (ISO 27001 — no hard deletes).
    """
    __tablename__ = "break_glass_use_requests"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    credential_id = Column(
        Integer, ForeignKey("break_glass_credentials.id"),
        nullable=False, index=True)
    # The platform admin requesting the emergency use.
    requested_by_user_id = Column(Integer, ForeignKey("users.id"),
                                  nullable=False, index=True)
    # Why the emergency use is needed (required, shown to the approver).
    reason = Column(Text, nullable=False)
    # 'pending' | 'approved' | 'denied' | 'expired' | 'used'.
    status = Column(String(16), nullable=False, default='pending',
                    index=True)
    # The platform admin who approved / denied (MUST differ from the
    # requester — enforced in services/break_glass_custody.approve).
    approved_by_user_id = Column(Integer, ForeignKey("users.id"),
                                 nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True,
                         index=True)
    # Approval deadline: approved_at + approval window (the
    # break_glass.approval_window_minutes platform setting). Past this,
    # an un-used approval is swept to 'expired'.
    expires_at = Column(DateTime(timezone=True), nullable=True,
                        index=True)
    # Single-use stamp (set exactly once by mark_used).
    used_at = Column(DateTime(timezone=True), nullable=True)
    used_by_user_id = Column(Integer, ForeignKey("users.id"),
                             nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    # SoftDeleteMixin adds `deleted_at` (nullable, indexed).

    __table_args__ = (
        Index("ix_break_glass_use_requests_cred_status",
              "credential_id", "status"),
    )


class SupportTicket(SoftDeleteMixin, Base):
    """In-app support ticket.

    One row per support request opened from the tenant. ``severity`` is
    the Q53 ladder (sev1..sev4); ``plan_tier`` is a SNAPSHOT of the
    tenant's plan tier at open time (``Plan.tier``: the edition's tier, NULL when the plan carries no bundle) so a
    later plan change never rewrites the SLA the ticket was opened
    under. ``first_response_due_at`` = opened_at + the hours the
    platform setting ``support_tickets.response_targets`` (services/
    support_tickets.py) assigns to (plan_tier, severity): Community best effort. Business-day math lives in
    services/support_tickets.first_response_due (Mon-Fri 9-17 UTC).

    ISO 27001: NO hard delete — removal stamps ``deleted_at``/
    ``deleted_by``; the row always stays.
    """
    __tablename__ = "support_tickets"
    # Same BigInteger/Integer variant trick as SecurityAlert so the test
    # suite's engine can autoincrement via ROWID while Postgres keeps
    # BIGINT.
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    # The user who opened the ticket (their email is the ack recipient).
    opened_by = Column(ForeignKey("users.id"), nullable=False, index=True)
    # sev1 | sev2 | sev3 | sev4 (Q53=A ladder).
    severity = Column(String(8), nullable=False, default="sev3",
                      server_default="sev3")
    subject = Column(String(255), nullable=False)
    body = Column(Text, nullable=True)
    # open | in_progress | resolved | closed.
    status = Column(String(16), nullable=False, default="open",
                    server_default="open")
    # Snapshot of Plan.tier at open time (NULL = community/unbunded).
    plan_tier = Column(String(20), nullable=True)
    first_response_due_at = Column(DateTime(timezone=True), nullable=True)
    first_responded_at = Column(DateTime(timezone=True), nullable=True)
    first_responded_by = Column(ForeignKey("users.id"), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by = Column(ForeignKey("users.id"), nullable=True)
    # Soft delete ONLY (ISO 27001 — no hard deletes).
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "severity IN ('sev1', 'sev2', 'sev3', 'sev4')",
            name="ck_support_tickets_severity"),
        CheckConstraint(
            "status IN ('open', 'in_progress', 'resolved', 'closed')",
            name="ck_support_tickets_status"),
        Index("ix_support_tickets_tenant_status", "tenant_id", "status"),
        Index("ix_support_tickets_first_response_due",
              "first_response_due_at"),
    )


# =========================================================================
# GSEC FX.69 part 2 (owner decision Q52=A; Q71=C) — hosted-uptime SLA
# measurement. One probe sample per component per minute (append-only
# telemetry recorded by the uptime sampling job in services/scheduler.py,
# reusing the /health probe fan-out in
# services/platform_health_service._run_all_checks); scheduled maintenance
# windows excluded per the Q52=A rules (>=72h notice, <=240 min/month).
# The monthly calculation lives in services/uptime.py. ISO 27001: samples
# are append-only; maintenance windows are soft-delete only.
# =========================================================================


class UptimeSample(Base):
    """GSEC FX.69 part 2 — one probe sample for one component.

    Append-only telemetry: the monthly SLA calculation reads these rows
    and never updates or deletes them. ``ok`` is False when the probe
    status was not ``up``; ``latency_ms`` is the probe's measured
    round-trip (NULL when the probe errored)."""
    __tablename__ = "uptime_samples"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    component = Column(String(64), nullable=False, index=True)
    ok = Column(Boolean, nullable=False, index=True)
    latency_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)

    __table_args__ = (
        Index("ix_uptime_samples_component_ts", "component", "ts"),
    )


class MaintenanceWindow(Base):
    """GSEC FX.69 part 2 — one scheduled-maintenance window.

    Excluded from SLA downtime ONLY when ``announced_at`` is at least
    72h before ``start_at`` (Q52=A) and the month's total exclusions
    stay within the 240 min cap; anything beyond counts as downtime.
    ``deleted_at`` is the soft-delete marker (ISO 27001 — no hard
    deletes)."""
    __tablename__ = "maintenance_windows"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    start_at = Column(DateTime(timezone=True), nullable=False, index=True)
    end_at = Column(DateTime(timezone=True), nullable=False)
    announced_at = Column(DateTime(timezone=True), nullable=True)
    reason = Column(String(500), nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, onupdate=func.now())
    # ISO 27001: soft delete only.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("end_at > start_at",
                        name="ck_maintenance_windows_end_after_start"),
    )


class LicenceRecord(SoftDeleteMixin, Base):
    """GSEC FX.73 part 1 (owner decision Q56=A) — third-party licence record.

    One row per licensable subject (a node definition, a model/weights
    repo, or a container image) carrying its detected code/model licence
    and a commercial-use classification. Auto-detected from SPDX
    (``services/licence_detect.py::classify``) or Hugging Face model-card
    metadata (``detect_from_hf``); ``source="manual"`` when a human set it
    directly, ``source="unknown"`` when nothing could be determined.
    ``commercial_use`` is ALWAYS ``unknown`` rather than guessed
    ``allowed`` when detection is ambiguous — downstream gates (FX.73 p3:
    hide non-commercial items from paid/hosted tenants) must fail closed.

    This feeds two later parts, not built here: FX.73 p2's manual-review
    queue reads rows with ``commercial_use`` in (``unknown``,
    ``restricted``) and ``reviewed_at IS NULL``; FX.73 p3's visibility
    gate and FX.73.p4's palette badge read ``commercial_use`` +
    ``spdx_id`` directly. FX.99's model/node licence labels are these
    same rows (no separate label table).

    Soft-delete only (ISO 27001 — no hard deletes): ``deleted_at`` marks
    a record superseded or retracted; the row is never removed.
    """
    __tablename__ = "licence_records"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # What this licence covers. 'node_definition' | 'model' | 'container_image'.
    # Kept as a free string (not an FK) so one table covers three unrelated
    # subject tables without three nullable FK columns.
    subject_type = Column(String(32), nullable=False, index=True)
    # The subject's natural id: node_definitions.id (as text), an HF/model
    # repo id ("stabilityai/sdxl-turbo"), or an image ref/digest.
    subject_id = Column(String(255), nullable=False, index=True)
    # SPDX identifier when known (e.g. "MIT", "Apache-2.0"); a vendor
    # licence slug that isn't in the SPDX list (e.g.
    # "tencent-hunyuan-community") is also stored here for traceability,
    # even though classify() only recognises a curated subset.
    spdx_id = Column(String(128), nullable=True)
    # Human-readable licence name, used when the licence has no SPDX id
    # (HF "other" + license_name, or a manually entered custom licence).
    licence_name = Column(String(255), nullable=True)
    # How this record was produced. 'spdx' | 'huggingface' | 'manual' | 'unknown'.
    source = Column(String(16), nullable=False, default="unknown",
                    server_default="unknown")
    # Commercial-use classification. 'allowed' | 'non_commercial' |
    # 'unknown' | 'restricted' ('restricted' = commercial use with
    # conditions — revenue caps, copyleft obligations, usage limits —
    # that need the FX.73 p2 manual-review queue before relying on it).
    commercial_use = Column(String(16), nullable=False, default="unknown",
                            server_default="unknown")
    # Whether the licence requires preserving an attribution/copyright
    # notice. NULL when unknown (never guessed).
    attribution_required = Column(Boolean, nullable=True)
    # When auto-detection ran. NULL for a pure-manual record.
    detected_at = Column(DateTime(timezone=True), nullable=True)
    # FX.73 p2 manual-review queue fields — nullable until reviewed.
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, onupdate=func.now())
    # SoftDeleteMixin adds `deleted_at` (nullable, indexed).

    __table_args__ = (
        Index("ix_licence_records_subject", "subject_type", "subject_id"),
        CheckConstraint(
            "source IN ('spdx', 'huggingface', 'manual', 'unknown')",
            name="ck_licence_records_source",
        ),
        CheckConstraint(
            "commercial_use IN ('allowed', 'non_commercial', 'unknown', 'restricted')",
            name="ck_licence_records_commercial_use",
        ),
    )


class TenantDeletionAttestation(Base):
    """
    GSEC FX.59 part 5b — append-only deletion attestation record.

    When a tenant's data is crypto-shredded (FX.59 p5a, not built yet —
    day-60 tenant-key crypto shred), the platform keeps ONE immutable
    fact of the deletion: who the tenant was (name snapshot, since the
    tenant row itself may later be gone), when deletion was requested
    and when the shred completed, the method used, the identifiers of
    the key material destroyed (identifiers ONLY — never key material),
    the acting user (NULL = system-initiated), and evidence (counts
    only).

    Append-only by design (ISO 27001 — no hard deletes, and this table
    has no soft-delete marker either: an attestation is a fact and can
    never be retracted). There is no update/delete path anywhere; the
    services/deletion_attestation.py service only records and lists.
    """
    __tablename__ = "tenant_deletion_attestations"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    tenant_name_snapshot = Column(String(255), nullable=False)
    requested_at = Column(DateTime(timezone=True), nullable=False)
    shredded_at = Column(DateTime(timezone=True), nullable=False)
    method = Column(String(64), nullable=False)
    key_ids_destroyed = Column(JSONB, nullable=False, default=list,
                               server_default=text("'[]'::jsonb"))
    actor_user_id = Column(Integer, nullable=True)
    evidence = Column(JSONB, nullable=False, default=dict,
                      server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    # tenant_id carries index=True -> ix_tenant_deletion_attestations_tenant_id
    # (same name the migration creates; no explicit Index here or create_all
    # would emit the index twice).


class NdaRequest(SoftDeleteMixin, Base):
    """GSEC FX.76 p5a — trust-centre "request NDA / security documentation"
    form submissions (routers/public_nda_requests.py).

    One row per public form submission (no authenticated user behind it):
    who asked (name/email/company/role free text), what they want
    (``documents_requested`` — library doc keys validated against the
    router's allowlist), the free-text use case (<=2000 chars, validated
    in the router), and the review lifecycle (status new|approved|declined
    with reviewed_by/reviewed_at filled by the FX.76 p5c review path).

    ``ip_hash`` is sha256(client-IP) hex — the raw IP is NEVER stored
    (GSEC: public endpoints keep only an unlinkable identifier for
    abuse/rate-limit correlation). deleted_at comes from SoftDeleteMixin
    (ISO 27001 — no hard deletes).
    """
    __tablename__ = "nda_requests"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    email = Column(String(255), nullable=False)
    company = Column(String(255), nullable=True)
    role = Column(String(120), nullable=True)
    use_case = Column(Text, nullable=True)
    documents_requested = Column(JSONB, nullable=False, default=list,
                                 server_default=text("'[]'::jsonb"))
    # 'new' | 'approved' | 'declined' (validated in the router/service).
    status = Column(String(20), nullable=False, default='new', index=True)
    ip_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    # Filled by the review path (FX.76 p5c); NULL until reviewed.
    reviewed_by = Column(ForeignKey("users.id"), nullable=True,
                         index=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    # deleted_at (SoftDeleteMixin) carries index=True ->
    # ix_nda_requests_deleted_at (same name the migration creates).


# =============================================================================
# --- GSEC FX.79 p1a — data-marking labels (CUI / ITAR / export-controlled)
# =============================================================================
# Owner decision Q62=A: admins mark workflows, files and outputs (CUI,
# CUI//SP-category, or a custom label). Marked items cannot leave the tenant
# (no external share / marketplace / external-AI), run only on tenant
# on-prem GPUs, are access audit-logged (DATA_MARKING_SET /
# DATA_MARKING_CLEARED) and ITAR-labelled items are restricted to US-person
# users (customer attests, MOD enforces via MarkingLabelDef.requires_us_person).
#
# data_markings = the live marks (soft-deleted on clear — ISO 27001, no hard
# deletes); marking_label_defs = the label catalogue (system rows with
# tenant_id NULL, tenant-defined rows per tenant).

DATA_MARKING_SYSTEM_LABELS = (
    "CUI",
    "CUI//SP-EXPT",
    "ITAR",
    "EAR99",
    "EXPORT_CONTROLLED",
    "PROPRIETARY",
)

# Resource kinds an admin may mark (Q62: "workflows, files and outputs").
DATA_MARKING_RESOURCE_TYPES = (
    "workflow",
    "asset",
    "node_definition",
    "named_artifact",
    "dataset",
)


class DataMarking(SoftDeleteMixin, Base):
    """GSEC FX.79 p1a — one data-marking label applied to one tenant resource.

    ``resource_type`` is one of DATA_MARKING_RESOURCE_TYPES (validated in
    the router; stored as plain VARCHAR so the set can grow with
    customer-defined resources); ``resource_id`` is the target row's id as
    a string (targets are heterogeneous — workflows.id, asset ids, …).
    ``label`` is a code from DATA_MARKING_SYSTEM_LABELS or a tenant-defined
    MarkingLabelDef.code. ``dissemination`` is the optional handling
    addendum (e.g. ``NOFORN`` / ``FED ONLY``) shown in the UI banner and
    stamped on exported documents.

    Uniqueness: one ACTIVE (non-deleted) mark per
    (tenant, resource_type, resource_id, label) — enforced by the partial
    unique index ``uq_data_markings_live`` (WHERE deleted_at IS NULL), so a
    soft-deleted mark does not block re-marking the same resource with the
    same label. deleted_at comes from SoftDeleteMixin (ISO 27001 — NO hard
    deletes; clearing a mark sets deleted_at and writes DATA_MARKING_CLEARED).
    """
    __tablename__ = "data_markings"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    resource_type = Column(String(40), nullable=False, index=True)
    resource_id = Column(String(64), nullable=False)
    label = Column(String(40), nullable=False, index=True)
    # e.g. "NOFORN", "FED ONLY"; NULL when no addendum.
    dissemination = Column(String(120), nullable=True)
    # Who applied the mark (admin); NULL only for platform-tenant seeding.
    set_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    set_at = Column(DateTime(timezone=True),
                    server_default=func.now(), nullable=False)
    reason = Column(Text, nullable=True)
    # deleted_at (SoftDeleteMixin) carries index=True ->
    # ix_data_markings_deleted_at (same name the migration creates).

    __table_args__ = (
        Index(
            "uq_data_markings_live",
            "tenant_id", "resource_type", "resource_id", "label",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )


class MarkingLabelDef(SoftDeleteMixin, Base):
    """GSEC FX.79 p1a — a data-marking label definition.

    tenant_id NULL = system label (platform-wide catalogue: CUI,
    CUI//SP-EXPT, ITAR, EAR99, EXPORT_CONTROLLED, PROPRIETARY — seeded by
    migrations/20261007zb_fx79_data_markings.py). tenant_id set = a
    customer-defined label scoped to that tenant.

    ``requires_us_person`` (ITAR, CUI//SP-EXPT): the mark may only be applied
    by / enforced against users with the admin-set US-person attribute —
    the customer attests US-person status, MOD enforces access, MOD does not
    verify identity (Q62). ``blocks_external_ai`` /
    ``blocks_external_share``: marked items may not be sent to external AI
    providers or shared outside the tenant — both default True and are
    honored by the FX.79 part-1b+ guards.
    """
    __tablename__ = "marking_label_defs"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # NULL = system label (visible to every tenant).
    tenant_id = Column(ForeignKey("tenants.id"), nullable=True)
    code = Column(String(40), nullable=False)
    description = Column(Text, nullable=True)
    requires_us_person = Column(Boolean, nullable=False, default=False,
                                server_default=text("FALSE"))
    blocks_external_ai = Column(Boolean, nullable=False, default=True,
                                server_default=text("TRUE"))
    blocks_external_share = Column(Boolean, nullable=False, default=True,
                                   server_default=text("TRUE"))
    # GSEC FX.79 p2b — marked items may not run on non-tenant (shared /
    # harvested / marketplace) GPUs: the run is restricted to the tenant's
    # own workers. Nullable so the p2b migration (20261007zg) can add the
    # column to pre-existing rows with NULL; readers treat NULL/absent as
    # False via `bool(defn.blocks_shared_gpu or False)`.
    blocks_shared_gpu = Column(Boolean, nullable=True, default=False,
                               server_default=text("FALSE"))
    created_at = Column(DateTime(timezone=True),
                        server_default=func.now(), nullable=False)
    # deleted_at (SoftDeleteMixin) carries index=True ->
    # ix_marking_label_defs_deleted_at (migration name).

    __table_args__ = (
        # System labels: at most one row per code with tenant_id NULL;
        # tenant labels: one row per (tenant, code).
        UniqueConstraint("tenant_id", "code",
                         name="uq_marking_label_defs_tenant_code"),
        Index("ix_marking_label_defs_code", "code"),
    )


# GSEC FX.88 p3 statuses — a terminal state (approved+completed, rejected,
# expired) is never re-entered.
MFA_RECOVERY_REQUEST_STATUSES = (
    "requested", "callback_verified", "approved", "rejected", "completed",
    "expired",
)
# Lazy-expiry window (checked on read/approve, no cron needed): a request
# older than this and still in requested/callback_verified is treated as
# expired.
MFA_RECOVERY_REQUEST_TTL_HOURS = 72


class MfaRecoveryRequest(SoftDeleteMixin, Base):
    """GSEC FX.88 p3 — last-admin MFA recovery request.

    Created by the public, unauthenticated
    `POST /v1/public/mfa-recovery-requests` endpoint
    (routers/public_mfa_recovery.py) ONLY when `requester_email` resolves to
    an active admin (``parent_user_id IS NULL``) of the target tenant AND
    that tenant has no OTHER active admin (the "last admin" gate) — every
    other case returns the same public 202 with no row written, so the
    endpoint never leaks which tenants/emails exist.

    ``security_contact_email`` is a SNAPSHOT of the tenant's registered
    security contact at creation time (services/tenant_security_contact_service.py
    resolution) — the notification email goes to this address, never to
    ``requester_email``, and the snapshot keeps the audit trail accurate even
    if the contact is later changed.

    Lifecycle (two-person, platform-admin only, routers/admin_mfa_recovery.py):
      requested -> callback_verified (staff phoned the security-contact
      number on file, routers/admin_mfa_recovery.py callback-verified) ->
      approved (a DIFFERENT platform admin approves — ``second_approver_user_id``
      must differ from ``callback_verified_by_user_id``, else 409) -> the
      approval performs the actual Keycloak reset (same helper
      routers/tenant/mfa_reset.py p2a uses) and flips the row to completed in
      the same transaction. ``rejected`` is a terminal dead-end (either staff
      step may reject). ``expired`` is set LAZILY — checked on read/approve
      against ``expires_at`` (created_at + MFA_RECOVERY_REQUEST_TTL_HOURS),
      never a background sweep — once expired the row can only be read, not
      approved/rejected.

    deleted_at (SoftDeleteMixin) — ISO 27001, no hard deletes; this table is
    append-only like every other audit-adjacent record in this codebase.
    """
    __tablename__ = "mfa_recovery_requests"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    target_user_id = Column(ForeignKey("users.id"), nullable=False, index=True)
    requester_email = Column(String(255), nullable=False)
    # Snapshot at creation — see class docstring.
    security_contact_email = Column(String(255), nullable=True)
    reason = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="requested",
                    server_default=text("'requested'"), index=True)

    callback_verified_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    callback_verified_at = Column(DateTime(timezone=True), nullable=True)
    callback_note = Column(Text, nullable=True)

    approved_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)

    rejected_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    rejected_at = Column(DateTime(timezone=True), nullable=True)
    rejection_reason = Column(Text, nullable=True)

    completed_at = Column(DateTime(timezone=True), nullable=True)

    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    # deleted_at (SoftDeleteMixin) carries index=True ->
    # ix_mfa_recovery_requests_deleted_at (migration name). tenant_id,
    # target_user_id and status each carry index=True above — their
    # ix_mfa_recovery_requests_{col} indexes come from that, NOT a
    # duplicate explicit Index() here (that raised DuplicateTable on
    # create_all).


# ---------------------------------------------------------------------------
# GSEC FX.85 p1a — subprocessor change log.
# ---------------------------------------------------------------------------


class SubprocessorChange(Base):
    """GSEC FX.85 p1a — append-only subprocessor change log.

    One row per announced change to the customer-facing subprocessor
    list (add / remove / replace / purpose_change): the subprocessor
    named, what changed (``change_type`` + free-text ``description``),
    the data categories affected (``data_categories`` JSONB), the
    region, when it was announced (``announced_at``) and when it
    takes effect (``effective_at``). The DEFAULT effective date is set
    by the service — announced_at + the ``subprocessor.notice_days``
    platform setting (default 30), the owner's "customers get at
    least 30 days' notice before a subprocessor is added" commitment
    (docs/security-audit/legal-drafts/subprocessors.md, row-9 note);
    the column is nullable so an emergency change may be recorded
    with an earlier explicit override (``is_emergency``).

    The canonical CURRENT list deliberately lives in the docs
    (docs-site/docs/legal/subprocessors.md, generated from
    docs/security-audit/legal-drafts/subprocessors.md / the vendor
    register) — this table records changes AGAINST that list and
    never duplicates it.

    ``is_ai_provider_tenant_toggled`` (Q70): AI providers a tenant
    toggles itself (external-LLM bindings, NIM cloud nodes) are
    exempt from the advance-notice obligation — the per-tenant
    opt-in gate is the notice.

    Append-only by design (ISO 27001 — no hard deletes, and an
    announcement is a fact, so there is no soft-delete marker
    either): no update/delete path anywhere;
    services/subprocessor_changes.py only records and lists.
    """
    __tablename__ = "subprocessor_changes"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    subprocessor_name = Column(String(255), nullable=False, index=True)
    # 'add' | 'remove' | 'replace' | 'purpose_change' — validated in
    # services/subprocessor_changes.py and by the CHECK constraint in
    # migrations/20261007zc_fx85_subprocessor_changes.py.
    change_type = Column(String(20), nullable=False, index=True)
    description = Column(Text, nullable=True)
    # Free-form JSON: a dict of category -> note, or a list of strings.
    data_categories = Column(JSONB, nullable=True)
    region = Column(String(120), nullable=True)
    announced_at = Column(DateTime(timezone=True), nullable=False,
                          index=True)
    # NULL = the service filled the default (announced_at +
    # subprocessor.notice_days) at record time; an explicit earlier
    # value = the emergency override.
    effective_at = Column(DateTime(timezone=True), nullable=True)
    is_emergency = Column(Boolean, nullable=False, default=False,
                          server_default=text("FALSE"))
    # Q70 — tenant-toggled AI providers are exempt from notices.
    is_ai_provider_tenant_toggled = Column(Boolean, nullable=False,
                                           default=False,
                                           server_default=text("FALSE"))
    created_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True),
                        server_default=func.now(), nullable=False)


class SubprocessorNoticeSubscription(Base):
    """GSEC FX.85 p1b (owner decision Q70=A) — a tenant admin's email
    subscription to subprocessor change notices.

    One row per (tenant, email) that has opted in to being emailed when a
    subprocessor change is announced (services/subprocessor_changes.py,
    part 2 fans the notices out to `active_recipients`). The tenant's own
    security contact (``tenants.security_contact_email``, FX.89) remains
    the fallback recipient when the tenant has NO active subscription.

    Soft-unsubscribe only (ISO 27001 — no hard deletes): unsubscribing
    stamps ``unsubscribed_at`` and leaves the row in place; a later
    re-subscribe is a NEW row, never a resurrection of the old one, so
    the subscribe/unsubscribe history is auditable in this table itself.

    Active uniqueness — one active row per (tenant, email, case
    insensitive) — is enforced in the service (services/
    subprocessor_changes.py::subscribe) AND by the partial unique index
    in migrations/20261007ze_fx85_notice_subscriptions.py
    (``(tenant_id, lower(email)) WHERE unsubscribed_at IS NULL``). The
    only index declared on the model is the ``tenant_id`` FK index
    (``index=True`` below) — no explicit ``Index``/``UniqueConstraint``
    here, so ``Base.metadata.create_all`` never double-indexes a column.
    """
    __tablename__ = "subprocessor_notice_subscriptions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    email = Column(String(255), nullable=False)
    subscribed_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True),
                        server_default=func.now(), nullable=False)
    # NULL = the email is an active recipient. Set (never cleared, never
    # the row deleted) when the tenant admin unsubscribes.
    unsubscribed_at = Column(DateTime(timezone=True), nullable=True)


class SubprocessorNoticeDelivery(Base):
    """GSEC FX.85 p2a (owner decision Q70=A) — append-only delivery log
    for subprocessor change notices.

    One row per DELIVERY ATTEMPT of one announced
    ``subprocessor_changes`` row to one recipient ``(tenant_id,
    email)`` (services/subprocessor_changes.py::send_change_notices).
    Idempotency: a ``status='sent'`` row for (change, tenant, email)
    means the recipient got the notice and is NEVER re-mailed; a
    ``status='failed'`` row means the send raised and is retried on
    the next daily scheduler run (each attempt appends its own row —
    the table is only ever appended to, ISO 27001 — and the partial
    unique index in migrations/20261007zf guarantees at most one
    'sent' row per recipient per change even across racing processes).
    """
    __tablename__ = "subprocessor_notice_deliveries"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    change_id = Column(ForeignKey("subprocessor_changes.id"),
                       nullable=False, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False,
                       index=True)
    email = Column(String(255), nullable=False)
    sent_at = Column(DateTime(timezone=True),
                     server_default=func.now(), nullable=False)
    # 'sent' (delivered) | 'failed' (send raised; retried next run).
    status = Column(String(16), nullable=False,
                    server_default=text("'sent'"))


class SubprocessorObjection(Base):
    """GSEC FX.85 p2b (owner decision Q70=A) — tenant objection handling
    for subprocessor changes.

    One row per tenant objection to one announced
    ``subprocessor_changes`` row, raised while the change's
    ``effective_at`` is still in the future (the 30-day notice window —
    the whole point of the advance notice). ``reason`` is the tenant's
    free-text objection (capped at 2000 chars in the service).

    Lifecycle (``status``, stamped transitions — the row itself is never
    deleted, ISO 27001):
        'open'                  raised, awaiting a platform response
        'acknowledged'          platform admin acknowledged it
        'resolved_accepted'     resolved; the platform stands by the
                                change (``resolved_at`` stamped)
        'resolved_withdrawn'    resolved; the platform withdraws /
                                revises the change (``resolved_at``
                                stamped)

    At most ONE 'open' row per (tenant, change) — enforced in the
    service (services/subprocessor_changes.py::raise_objection, which
    returns the existing open row idempotently) AND by the partial
    unique index in migrations/20261007zg_fx85_objections.py.
    ``platform_response`` carries the admin's reply text (nullable
    until one is given). ``updated_at`` is stamped on every transition
    (the initial 'open' row keeps it NULL — created_at is the raise
    time).
    """
    __tablename__ = "subprocessor_objections"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    change_id = Column(ForeignKey("subprocessor_changes.id"),
                       nullable=False, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False,
                       index=True)
    raised_by_user_id = Column(ForeignKey("users.id"), nullable=True)
    # The tenant's objection, free text (service-capped at 2000 chars).
    reason = Column(Text, nullable=False)
    # 'open' | 'acknowledged' | 'resolved_accepted' |
    # 'resolved_withdrawn' — validated in the service and by the CHECK
    # constraint in migrations/20261007zg_fx85_objections.py.
    status = Column(String(32), nullable=False,
                    server_default=text("'open'"))
    # The platform admin's reply (null until respond() is called).
    platform_response = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True),
                        server_default=func.now(), nullable=False)
    # NULL on the initial 'open' row (created_at is the raise time);
    # stamped on every status transition.
    updated_at = Column(DateTime(timezone=True), nullable=True)
    # Stamped when the objection leaves 'open' toward a resolved_*
    # status; NULL while open/acknowledged.
    resolved_at = Column(DateTime(timezone=True), nullable=True)


class ExportApprovalRequest(Base):
    """GSEC FX.161.p2a (owner decision Q148=A, DLP egress) — approval
    workflow for FULL tenant exports under a regulated compliance profile
    (services/export_approval.py).

    A tenant on a regulated profile (see dlp.export_approval_profiles)
    cannot run the full tenant export (routers/tenant/export.py, FX.59)
    directly: POST /v1/tenant/export creates THIS row (status 'pending')
    instead of starting the job. A DIFFERENT tenant admin approves or
    rejects it — the requester can never approve their own request (the
    service refuses, 403). Only an approved, unexpired request lets the
    export run, and exactly once: consumption stamps ``used_at``.

    Lifecycle (``status``; the row is NEVER hard-deleted — ISO 27001,
    removal is a ``deleted_at`` stamp only):
        'pending'   created, awaiting a second admin's decision
        'approved'  a different tenant admin approved (``decided_at``
                    stamped); valid until ``expires_at``
        'rejected'  a different tenant admin rejected (``decided_at``
                    stamped); raises the FX.156 alert
        'expired'   'pending'/'approved' past ``expires_at`` (stamped
                    lazily on the next read/attempt); an approved+expired
                    request never authorizes an export and raises the
                    FX.156 alert

    ``scope`` is the stable export-scope key (today:
    "full_tenant_export"); ``reason`` is the requester's free text.
    ``approver_user_id``/``decision_reason``/``decided_at`` carry the
    decision; ``used_at`` is stamped when the one-shot export runs.
    """
    __tablename__ = "export_approval_requests"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False,
                       index=True)
    requested_by = Column(ForeignKey("users.id"), nullable=False,
                          index=True)
    # Stable export-scope key, e.g. "full_tenant_export".
    scope = Column(String(64), nullable=False)
    # The requester's free-text justification (service-capped at 2000).
    reason = Column(Text, nullable=True)
    # 'pending' | 'approved' | 'rejected' | 'expired' — validated in the
    # service and by the CHECK constraint in
    # migrations/20261007zi_fx161_export_approvals.py.
    status = Column(String(32), nullable=False,
                    server_default=text("'pending'"), index=True)
    # The SECOND tenant admin who decided (NULL while pending).
    approver_user_id = Column(ForeignKey("users.id"), nullable=True,
                              index=True)
    decision_reason = Column(Text, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    # Deadline for the pending request AND for an approved one to be
    # consumed; set from dlp.export_approval_expiry_hours at creation.
    expires_at = Column(DateTime(timezone=True), nullable=True,
                        index=True)
    # Stamped exactly once when the authorized export runs (one-shot).
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True),
                        server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    # ISO 27001: soft delete ONLY — no hard delete path exists.
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        Index("ix_export_approval_requests_tenant_created",
              "tenant_id", "created_at"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="ck_export_approval_requests_status"),
    )


# =============================================================================
# --- GSEC FX.158 p2a (owner decision Q145=A) — security advisories (CVE IDs)
# =============================================================================
# Platform-admin-authored advisories, published to the public trust-page feed
# (routers/public_security_advisories.py). Draft = published_at IS NULL: a
# draft is visible to platform admins only and never appears in the public
# feed. Table created by migrations/20261007zo_fx158_p2a_advisories.py.

class SecurityAdvisory(SoftDeleteMixin, Base):
    """GSEC FX.158 p2a — one published security advisory.

    ``advisory_id`` is the human-facing identifier (``MOD-SA-2026-001``) and
    is unique. ``cve_ids`` is a JSONB list of CVE identifiers, each validated
    against ``CVE-\\d{4}-\\d{4,}`` in services/security_advisories.py (never
    read with ``.astext`` — JSONB is used as a Python list).
    ``affected_versions`` is a JSONB list of affected release/version
    strings; ``fixed_in_version`` is the version that carries the fix.

    ``published_at`` NULL = DRAFT (platform-admin view only; the public feed
    returns published, non-soft-deleted rows newest-first).

    ISO 27001: no hard deletes — ``deleted_at`` (SoftDeleteMixin, indexed as
    ix_security_advisories_deleted_at) is the soft-delete marker, and every
    write path writes an append-only ``AuditAction.SECURITY_ADVISORY_*`` row.
    """
    __tablename__ = "security_advisories"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    advisory_id = Column(String(40), nullable=False, unique=True)
    title = Column(String(200), nullable=False)
    # 'critical' | 'high' | 'medium' | 'low' (service-validated + CHECK
    # constraint in the migration).
    severity = Column(String(16), nullable=False, index=True)
    cve_ids = Column(JSONB, nullable=False, default=list,
                     server_default=text("'[]'::jsonb"))
    affected_versions = Column(JSONB, nullable=False, default=list,
                               server_default=text("'[]'::jsonb"))
    fixed_in_version = Column(String(120), nullable=True)
    summary = Column(Text, nullable=False)
    # NULL = draft (hidden from the public feed); stamped at publish.
    published_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(ForeignKey("users.id"), nullable=True)
    published_by = Column(ForeignKey("users.id"), nullable=True)
    # deleted_at (SoftDeleteMixin) carries index=True ->
    # ix_security_advisories_deleted_at (same name the migration creates).

    __table_args__ = (
        CheckConstraint(
            "severity IN ('critical', 'high', 'medium', 'low')",
            name="ck_security_advisories_severity"),
        Index("ix_security_advisories_published_desc",
              "published_at", "created_at"),
    )


# =============================================================================
# --- GSEC FX.163 p1 (owner decision Q150=A) — segregation of duties / maker-checker
# =============================================================================
# Table created by migrations/20261007zv_fx163_p1_pending_approvals.py.


class PendingApproval(SoftDeleteMixin, Base):
    """GSEC FX.163 p1 (owner decision Q150=A) — one pending second-party
    approval for a sensitive tenant action (segregation of duties / maker-checker).

    A tenant action that can quietly widen access — a role grant, a FULL
    tenant export, a BYOK revoke, a data-marking clear — is recorded here
    when the tenant setting ``approvals.require_second_approver`` is ON
    (or an active compliance profile forces it ON; see
    services/approvals.approvals_required). The action itself does NOT run
    until a DIFFERENT tenant admin approves this row: the CHECK constraint
    ``ck_pending_approvals_second_approver`` makes the DB itself refuse
    ``approved_by_user_id == requested_by_user_id``, so a self-approval
    cannot be written even by a raw INSERT.

    ``action`` is the stable action key (today: "role_grant" | "full_export"
    | "byok_revoke" | "data_marking_clear"); ``payload`` is the JSONB detail
    the gated action needs to replay (identifiers only — never a secret) and
    ``payload_sha256`` is its canonical-JSON digest, so the approval proves
    THAT exact payload was the one approved (the service re-hashes at
    execution and refuses on a mismatch).

    Lifecycle (``status``; the row is NEVER hard-deleted — ISO 27001,
    removal is a ``deleted_at`` stamp only):
        'pending'   created, awaiting a second admin's decision
        'approved'  a DIFFERENT tenant admin approved (``decided_at``
                    stamped); valid until ``expires_at``
        'rejected'  a DIFFERENT tenant admin rejected (``decided_at``
                    stamped)
        'expired'   'pending'/'approved' past ``expires_at`` (stamped
                    lazily on the next read/attempt)
        'cancelled' withdrawn by the requester before any decision

    ``reason`` is the requester's free text; ``decision_note`` the
    approver's. Both actors are audited by the service (FX.163 p2).
    """
    __tablename__ = "pending_approvals"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False,
                       index=True)
    # Stable action key, e.g. "role_grant" | "full_export" |
    # "byok_revoke" | "data_marking_clear" (service-validated).
    action = Column(String(64), nullable=False, index=True)
    # The gated action's replay detail (identifiers only).
    payload = Column(JSONB, nullable=False, default=dict,
                     server_default=text("'{}'::jsonb"))
    # sha256 of the canonical JSON of ``payload`` (service-computed).
    payload_sha256 = Column(String(64), nullable=False)
    # The human who wants the action to run.
    requested_by_user_id = Column(ForeignKey("users.id"), nullable=False,
                                  index=True)
    requested_at = Column(DateTime(timezone=True),
                          server_default=func.now(), nullable=False,
                          index=True)
    # The SECOND tenant admin who decided (NULL while pending). Never
    # equal to requested_by_user_id — see ck_pending_approvals_second_approver.
    approved_by_user_id = Column(ForeignKey("users.id"), nullable=True,
                                 index=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    # 'pending' | 'approved' | 'rejected' | 'expired' | 'cancelled' —
    # validated in the service and by the CHECK constraint in
    # migrations/20261007zv_fx163_p1_pending_approvals.py.
    status = Column(String(32), nullable=False,
                    server_default=text("'pending'"), index=True)
    # Deadline: while pending, and for an approved one until the gated
    # action actually runs (from dlp.export_approval_expiry_hours-style
    # expiry; set by the service at creation).
    expires_at = Column(DateTime(timezone=True), nullable=True,
                        index=True)
    reason = Column(Text, nullable=True)
    decision_note = Column(Text, nullable=True)
    # deleted_at (SoftDeleteMixin) carries index=True ->
    # ix_pending_approvals_deleted_at (same name the migration creates).

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', "
            "'expired', 'cancelled')",
            name="ck_pending_approvals_status"),
        # Maker-checker at the DB itself: an approval row can never name
        # the requester as its approver. NULL (still pending) is allowed.
        CheckConstraint(
            "approved_by_user_id IS NULL OR "
            "approved_by_user_id != requested_by_user_id",
            name="ck_pending_approvals_second_approver"),
        Index("ix_pending_approvals_tenant_requested",
              "tenant_id", "requested_at"),
        Index("ix_pending_approvals_tenant_status",
              "tenant_id", "status"),
    )


class PublisherVerification(SoftDeleteMixin, Base):
    """GSEC FX.95 p4a (owner decision Q81=A) — the publisher identity/company
    verification record behind the marketplace Verified badge.

    One row per publisher tenant (the tenant/org that owns marketplace
    listings — `WorkflowListing.publisher_tenant_id` /
    `StackListing.publisher_tenant_id`). A publisher asks for verification with
    an evidence note; MOD staff (a platform admin) then approves, rejects, or
    later revokes it. The badge (FX.95 p4b) renders from `status == 'verified'`.

    Lifecycle (`status`; the row is NEVER hard-deleted — ISO 27001, removal is
    a `deleted_at` stamp only, and every transition is an append-only audit row
    via `audit.log_security_event`, same convention as services/listing_review.py):

        'unverified'  starting state (also what a REJECT falls back to)
        'pending'     publisher requested, awaiting a staff decision
        'verified'    staff verified the identity/company evidence
        'revoked'     staff withdrew a live verification

    `method` records HOW staff verified (domain_dns | business_registry |
    manual_documents | other). `evidence_note` is the publisher's capped free
    text, `decision_note` the staff's, `revoked_reason` the revoke reason.
    `verified_by` is the staff users.id that approved; `revoked_by` the one that
    revoked. Revoke keeps the whole history: `verified_at`/`verified_by` stay
    stamped on the row, so the record still shows who verified it and when even
    after the badge is withdrawn.

    Additive only: nullable columns, no existing table touched. Schema is
    created by migrations/20261007zy_fx95_p4a_publisher_verification.py.
    One record per publisher tenant is enforced by
    uq_publisher_verifications_publisher_tenant_id.
    """
    __tablename__ = "publisher_verifications"

    id = Column(Integer, primary_key=True)
    publisher_tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                                 nullable=False, index=True)
    # 'unverified' | 'pending' | 'verified' | 'revoked' — validated in the
    # service and by ck_publisher_verifications_status in the migration.
    status = Column(String(20), nullable=False,
                    server_default=text("'unverified'"), index=True)
    # How staff verified: domain_dns | business_registry | manual_documents |
    # other (service-validated; NULL while unverified/pending).
    method = Column(String(32), nullable=True)
    # Publisher's evidence note (capped at MAX_EVIDENCE_NOTE_CHARS in the
    # service); the free text is not the evidence document itself.
    evidence_note = Column(Text, nullable=True)
    # The staff decision note (approve/reject) and the revoke reason.
    decision_note = Column(Text, nullable=True)
    requested_at = Column(DateTime(timezone=True), nullable=True)
    requested_by_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"),
                                  nullable=True, index=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by = Column(ForeignKey("users.id", ondelete="SET NULL"),
                         nullable=True, index=True)
    rejected_at = Column(DateTime(timezone=True), nullable=True)
    rejected_by = Column(ForeignKey("users.id", ondelete="SET NULL"),
                         nullable=True, index=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by = Column(ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True, index=True)
    revoked_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('unverified', 'pending', 'verified', 'revoked')",
            name="ck_publisher_verifications_status"),
        # One index definition per column: the indexed columns carry index=True
        # (which names them ix_publisher_verifications_<column>), so no explicit
        # Index() here — declaring both makes conftest's create_all fail with a
        # duplicate-name error. The migration emits the same names with
        # CREATE INDEX IF NOT EXISTS, and the same unique-constraint name.
        UniqueConstraint(
            "publisher_tenant_id", name="uq_publisher_verifications_publisher_tenant_id"),
    )


# =============================================================================
# --- GSEC FX.144 p2a (owner decision Q131=A) — production release approval
# =============================================================================
# Table created by migrations/20261007zzy_fx144_p2a_release_approvals.py.
# See docs/isms/09a-emergency-change-procedure.md for the procedure this row
# records (emergency review window = 24h, owner decision 10-07).


class ReleaseApproval(SoftDeleteMixin, Base):
    """GSEC FX.144 p2a (owner decision Q131=A) — one production release/change
    and the owner decision on it (change control: agents author, owner approves).

    A production release (a signed tag ref + the container image digests it ships)
    is recorded here BEFORE it runs; the owner (a DIFFERENT user than the requester
    — the maker-checker rule services/approval_gate.py enforces for FX.163, restated
    here as the DB CHECK ``ck_release_approvals_second_approver``) approves or rejects
    it. The approve/reject decision carries the EVIDENCE the owner clicked through:
    the diff summary and the CI/test results, stored as JSON (identifiers + counts,
    never secrets).

    Emergency path (09a-emergency-change-procedure.md): a change that had to ship
    before review is stamped ``emergency`` with ``deployed_at`` and a
    ``review_due_at`` = deployed_at + ``change_mgmt.emergency_review_hours``
    (platform setting, default 24 — the doc's written constant, now a setting).
    Past that deadline the row is NOT "unreviewed and fine": the service stamps
    ``review_status`` 'overdue' (the next action is rollback to the previous tag,
    kept in ``rollback_ref``).

    Lifecycle (``status``; the row is NEVER hard-deleted — ISO 27001, removal is a
    ``deleted_at`` stamp only, and every transition is an append-only audit row via
    ``audit.log_security_event`` reusing existing AuditAction values, so there is no
    new enum member and no status-history table):
        'pending'    requested, awaiting the owner's one-click decision
        'approved'   a DIFFERENT user approved (``approved_by``/``approved_at``)
        'rejected'   a DIFFERENT user rejected (``approved_by``/``approved_at``
                     carry the decider; the release never runs)
        'emergency'  shipped without prior approval, awaiting retroactive review
        'superseded' withdrawn by the requester before any decision

    ``review_status`` ('pending' | 'reviewed' | 'overdue') tracks the EMERGENCY
    review only — a normal approved release has nothing left to review.

    Schema is created by migrations/20261007zzy_fx144_p2a_release_approvals.py.
    """
    __tablename__ = "release_approvals"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The release/tag ref this record gates (e.g. "v1.0.77", signed tag).
    release_ref = Column(String(128), nullable=False, index=True)
    # The previous release ref to roll back to (09a: written BEFORE an emergency
    # deploy; NULL for a first release).
    rollback_ref = Column(String(128), nullable=True)
    # The image digests this release ships: {"service": "sha256:<hex>", ...}
    # (identifiers only — never key material).
    image_digests = Column(JSONB, nullable=False, default=dict,
                           server_default=text("'{}'::jsonb"))
    # The CI/test evidence the approver read ({"ci": {...}, "tests": {...}}).
    evidence = Column(JSONB, nullable=False, default=dict,
                      server_default=text("'{}'::jsonb"))
    # The human who wants the release to run (an agent or a tenant/platform admin).
    requested_by_user_id = Column(ForeignKey("users.id"), nullable=False,
                                  index=True)
    requested_at = Column(DateTime(timezone=True),
                          server_default=func.now(), nullable=False,
                          index=True)
    # The SECOND human who decided (MUST differ from the requester — see
    # ck_release_approvals_second_approver). NULL while pending/emergency.
    approved_by_user_id = Column(ForeignKey("users.id"), nullable=True,
                                 index=True)
    approved_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # 'pending' | 'approved' | 'rejected' | 'emergency' | 'superseded' —
    # validated in the service and by the CHECK constraint in
    # migrations/20261007zzy_fx144_p2a_release_approvals.py.
    status = Column(String(32), nullable=False,
                    server_default=text("'pending'"), index=True)
    # The requester's free-text reason (09a "Reason" field, service-capped).
    reason = Column(Text, nullable=True)
    # The decider's note (09a "Approver" verdict: keep / replace / revert).
    decision_note = Column(Text, nullable=True)
    # Emergency path: when the change actually deployed (NULL for a normal
    # release that was approved before running).
    deployed_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Emergency review deadline = deployed_at + change_mgmt.emergency_review_hours
    # (default 24h, owner decision 10-07). NULL unless this is an emergency row.
    review_due_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Emergency review state: 'pending' (awaiting review) | 'reviewed' (the owner
    # recorded a verdict) | 'overdue' (past review_due_at — rollback is next).
    # Stamped lazily, never by a delete.
    review_status = Column(String(16), nullable=False,
                           server_default=text("'pending'"), index=True)
    # deleted_at (SoftDeleteMixin) carries index=True ->
    # ix_release_approvals_deleted_at (same name the migration creates).

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', "
            "'emergency', 'superseded')",
            name="ck_release_approvals_status"),
        CheckConstraint(
            "review_status IN ('pending', 'reviewed', 'overdue')",
            name="ck_release_approvals_review_status"),
        # Maker-checker at the DB itself (the FX.163 p1 pattern): a release
        # record can never name its own requester as the approver, so a raw
        # INSERT cannot self-approve a production change.
        CheckConstraint(
            "approved_by_user_id IS NULL OR "
            "approved_by_user_id != requested_by_user_id",
            name="ck_release_approvals_second_approver"),
        # An emergency row always carries its deploy time and its deadline.
        CheckConstraint(
            "status != 'emergency' OR "
            "(deployed_at IS NOT NULL AND review_due_at IS NOT NULL)",
            name="ck_release_approvals_emergency_clocks"),
        # The single-indexed columns (release_ref, requested_at, status,
        # approved_by_user_id, deployed_at, review_due_at, review_status,
        # deleted_at) carry index=True -> ix_release_approvals_<column>, so
        # only the composites are declared here (declaring both for one name
        # makes conftest's create_all fail with a duplicate-name error).
        Index("ix_release_approvals_ref_status", "release_ref", "status"),
    )


class NodeLockdownException(SoftDeleteMixin, Base):
    """GSEC FX.115 p3 — a per-node lockdown exception with a RECORDED reason.

    FX.115 p1a/p1b gave the worker eight node-container lockdown knobs and one
    blunt exception surface: the comma-separated allow-list setting
    ``worker.node_lockdown_exempt_nodes``, which exempts a node kind from EVERY
    flag at once and carries no reason, no author, and no expiry. Compliance
    (ISO 27001 / the compliance vertical) needs the opposite shape: "this node
    kind relaxes THESE flags, because THIS reason, decided by THIS admin,
    until THEN" — so the exemption itself is an auditable record, not a
    string in a setting.

    Keyed on ``node_kind`` because that is what the worker's exempt() matches
    on today (transport.TaskPayload.node_kind, trimmed, case-insensitive) —
    the same key node_lockdown_verdicts uses, so the report and the exception
    list speak the same language. One row per (node kind, flag) mirrors how
    the verdict table stores flags, so "relax capdrop but keep nonroot" is
    expressible and the report can name the reason per flag.

    ``reason`` is REQUIRED (the router refuses blank): an exemption nobody
    can justify is exactly the finding a compliance audit reports. ``expires_at``
    is optional — a live row past its expiry is ignored (the API drops it from
    the derived tunable, so the worker stops seeing it) rather than deleted.

    Delivery: services/node_lockdown_report.effective_exempt_nodes() unions
    these live, unexpired rows with the legacy allow-list setting and the pair
    is pushed on the heartbeat ``tunables`` map as the DERIVED key
    ``worker.node_lockdown_exempt_list`` (same path as every other worker
    tunable — routers/worker_management._resolve_worker_tunables). The legacy
    setting keeps working as a fallback/union for one release so nothing
    breaks when the new list lands.

    ISO 27001: NO hard delete — revoke is a soft delete (deleted_at tombstone)
    and the uniqueness index is PARTIAL on ``deleted_at IS NULL`` (the
    node_egress_approvals idiom) so a tombstone never squats the live
    (node_kind, flag) key.
    """
    __tablename__ = "node_lockdown_exceptions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The subject: NodeDefinition.node_kind / transport.TaskPayload.node_kind,
    # trimmed. Matching is case-insensitive on the worker side.
    node_kind = Column(String(250), nullable=False, index=True)
    # One of the eight lockdown flags (nonroot / capdrop / noprivs / seccomp /
    # rootfs / limits_pids / limits_memory / limits_cpu), or "scratch_tmpfs"
    # for the writable-scratch set the rootfs knob adds — the SAME spelling
    # node_lockdown_verdicts stores, so one row relaxes exactly one flag.
    flag = Column(String(32), nullable=False, index=True)
    # REQUIRED justification. Blank/whitespace is refused by the router
    # (400) and by the service (ValueError), so a raw INSERT cannot sneak
    # an unjustified exemption in either.
    reason = Column(Text, nullable=False)
    # The platform admin who created it (audit trail + the report can name
    # who exempted what).
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True,
                                index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    # Optional end-of-exception. NULL = permanent. A live row whose expiry is
    # in the past is IGNORED (not deleted) until the admin revokes it.
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Soft-delete tombstone (SoftDeleteMixin) + who revoked, so the audit
    # trail names the actor as well as the audit_events row.
    deleted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        # One live exception per (node kind, flag): re-creating refreshes the
        # reason/expiry in place instead of stacking exemptions.
        Index(
            "uq_node_lockdown_exceptions_live_kind_flag",
            "node_kind", "flag",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )


class PrivilegedElevationRequest(SoftDeleteMixin, Base):
    """GSEC FX.120 p2a (owner decision Q107=A) — JIT production admin
    elevation: the DATA layer behind "no standing admin on production
    infrastructure".

    Production MOD runs in the colo cage (owner-operated infra is a
    separate concern): nobody gets a permanent host shell, database-admin
    console, Vault admin, or Docker socket. When an operator genuinely
    needs one, they file an ELEVATION REQUEST here instead of holding the
    key: a REQUIRED reason, exactly one of the four scopes, and a TTL
    (default 2h) that AUTO-REVOKES. Approval is second-person by default —
    a DIFFERENT platform admin — except in solo mode, where the operator
    self-approves and the fact is alerted AND audited (``self_approved``
    is the flag that makes a self-approval distinguishable from a
    colleague's, so the weekly FX.71 access report can never blur the two).

    PLATFORM scope, not tenant scope: this is MOD platform staff acting on
    MOD's own production infrastructure, so there is no ``tenant_id``
    column — same shape as ``BreakGlassUseRequest`` (FX.49), whose audit
    rows are written against the platform tenant (``PLATFORM_TENANT_ID``)
    by the service.

    ``scope`` is one of the four elevation kinds (service-validated +
    DB-enumerated by ck_..._scope):
        'host_shell'   a shell on the production host
        'db_admin'     the database-admin console (DB admin creds)
        'vault_admin'  Vault admin (dynamic creds issued for the window)
        'docker'       the Docker socket

    Status lifecycle (``status``; the row is NEVER hard-deleted — ISO 27001,
    removal is a ``deleted_at`` stamp only):
        'pending'   created, awaiting a decision
        'approved'  decided yes (``approved_at`` + ``approved_by_user_id``
                    stamped, ``self_approved`` says by whom); the window has
                    NOT opened yet
        'denied'    decided no (terminal)
        'active'    the approved window is OPEN (``activated_at`` +
                    ``expires_at`` = activated_at + TTL); the only state that
                    grants anything
        'expired'   TTL passed (from 'active') or the approval window lapsed
                    (from 'pending') -> auto-revoked
        'revoked'   an admin closed the window early (``revoked_at`` +
                    ``revoke_reason``)

    Illegal transitions are refused by the service (e.g. denied -> active,
    active -> approved, a terminal state -> anything).

    SOFT-DELETE ONLY (ISO 27001 — no hard deletes, no ONDELETE CASCADE on
    any FK here): the row is the compliance record even after deletion.
    """
    __tablename__ = "privileged_elevation_requests"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The platform admin filing the request (users.id).
    requested_by_user_id = Column(Integer, ForeignKey("users.id"),
                                  nullable=False, index=True)
    # One of the four elevation scopes (see the docstring); the DB
    # enumerates them so a raw INSERT cannot invent a fifth.
    scope = Column(String(16), nullable=False, index=True)
    # REQUIRED justification (blank/short is refused by the service AND by
    # ck_..._reason, so an unjustified elevation cannot exist in the DB).
    reason = Column(Text, nullable=False)
    # Requested window in MINUTES (default 120 = the owner decision's 2h).
    # The service clamps against the security.elevation_max_ttl_minutes
    # platform setting; the DB keeps one hard ceiling (10080 = 1 week) so
    # no row can claim a year-long window.
    requested_ttl_minutes = Column(Integer, nullable=False,
                                   server_default=text("120"), index=True)
    # 'pending' | 'approved' | 'denied' | 'active' | 'expired' | 'revoked'.
    status = Column(String(16), nullable=False,
                    server_default=text("'pending'"), index=True)
    # Who decided (approve/deny). In solo mode this EQUALS
    # requested_by_user_id — deliberately NOT a ck_second_approver CHECK
    # (unlike FX.163/FX.49): solo mode is a legal self-approval, and
    # ``self_approved`` is what names it as such.
    approved_by_user_id = Column(Integer, ForeignKey("users.id"),
                                 nullable=True, index=True)
    # True when the approval was the requester's own (solo mode). Kept
    # consistent with approved_by_user_id by ck_..._self_approved.
    self_approved = Column(Boolean, nullable=False,
                           server_default=text("false"), index=True)
    approved_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # The window actually opened (approved -> active).
    activated_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Auto-revoke deadline: activated_at + TTL while 'active'; for a
    # 'pending' row it is the approval-window deadline that lapses to
    # 'expired'.
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Why the window was closed early (required for a revoke).
    revoke_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    # SoftDeleteMixin adds `deleted_at` (nullable, indexed) —
    # ix_privileged_elevation_requests_deleted_at, the same name the
    # migration creates.

    __table_args__ = (
        CheckConstraint(
            "scope IN ('host_shell', 'db_admin', 'vault_admin', 'docker')",
            name="ck_privileged_elevation_requests_scope"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied', "
            "'active', 'expired', 'revoked')",
            name="ck_privileged_elevation_requests_status"),
        # A reason nobody can read is the finding a compliance audit reports.
        CheckConstraint(
            "LENGTH(TRIM(reason)) >= 10",
            name="ck_privileged_elevation_requests_reason"),
        CheckConstraint(
            "requested_ttl_minutes > 0 AND "
            "requested_ttl_minutes <= 10080",
            name="ck_privileged_elevation_requests_ttl"),
        # An OPEN window always carries its clocks.
        CheckConstraint(
            "status != 'active' OR "
            "(activated_at IS NOT NULL AND expires_at IS NOT NULL)",
            name="ck_privileged_elevation_requests_active_clocks"),
        # A revoked window always says when and why.
        CheckConstraint(
            "status != 'revoked' OR "
            "(revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)",
            name="ck_privileged_elevation_requests_revoked_clocks"),
        # A decision always stamps when and who — except 'expired', which is
        # how a LAPSED window reads (a 'pending' row whose approval window
        # passed with no decision at all has no approver to name).
        CheckConstraint(
            "status = 'pending' OR status = 'expired' OR "
            "approved_at IS NOT NULL",
            name="ck_privileged_elevation_requests_decision_clocks"),
        # solo-mode consistency: a self-approval names the requester, and
        # only a self-approval may do so.
        CheckConstraint(
            "self_approved = true OR "
            "approved_by_user_id != requested_by_user_id",
            name="ck_privileged_elevation_requests_self_approved"),
        Index("ix_privileged_elevation_requests_requested_status",
              "requested_by_user_id", "status"),
        Index("ix_privileged_elevation_requests_scope_status",
              "scope", "status"),
    )


class OncallPage(SoftDeleteMixin, Base):
    """GSEC FX.119 p3a (owner decision Q106=A) — one PAGE, as a RECORD.

    p2a shipped the paging CLIENT (services/paging_service.py: a page is a
    side effect beside the monitor's own audit row). p3a makes the page an
    object so it can be ACKED and ESCALATED: the on-call person's phone gets
    a tap-this-link ack URL, and if nobody taps it inside
    oncall.ack_window_seconds (default 900 = the owner decision's 15 min)
    the scheduler tick re-pages oncall.owner_topic exactly once.

    NEW TABLE ONLY (models.py is bind-mounted live: no column was added to
    any existing table). SOFT-DELETE ONLY (ISO 27001 — the page is the
    compliance record of "we told someone about this incident at this time",
    so removal is a `deleted_at` stamp; no ON DELETE CASCADE on any FK).

    ``ack_token_hash`` is the only place the ack token exists: a random
    secrets-token is handed to the caller ONCE (in the page's Click/Actions
    header) and only its SHA-256 hash is stored, so a leaked page record
    cannot mint an ack and the token is never comparable in plaintext
    (constant-time compare in the service). Single-use: the ack is legal
    only while status == 'sent', so a replayed link is refused (409).

    Status lifecycle (never hard-deleted):
        'sent'       paged the current on-call (the ack window is OPEN)
        'acked'      the on-call person tapped the link (``acked_at`` +
                     ``acked_by`` — the acking user's id, 0 = the token
                     carried no resolvable identity)
        'escalated'  the window lapsed: re-paged oncall.owner_topic once
                     (``escalated_at`` + ``escalated_to``)
        'failed'     the page never reached anyone (no channel / no token /
                     ntfy 4xx-5xx) — nothing to ack, nothing to escalate

    ``tenant_id`` is NULLABLE: monitoring pages are platform-level (tenant
    NULL, audit rows written against the platform tenant) while an
    Enterprise Sev-1 ticket page carries its tenant.
    """
    __tablename__ = "oncall_pages"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The monitor's dedupe key (same value paging_service buckets on), so
    # the escalation sweep and the dedupe read agree on one incident.
    dedupe_key = Column(String(128), nullable=True, index=True)
    title = Column(String(200), nullable=False)
    # ntfy priority actually used (1 low .. 10 urgent).
    priority = Column(Integer, nullable=False, server_default=text("5"))
    # NULL = platform-level page; otherwise the tenant that owns the incident.
    tenant_id = Column(ForeignKey("tenants.id"), nullable=True, index=True)
    # The ntfy topic the page was published to (the current on-call's topic,
    # or oncall.push_topic when no rota is configured).
    sent_to = Column(String(128), nullable=True, index=True)
    sent_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # SHA-256 hex of the single-use ack token. Never the token itself.
    ack_token_hash = Column(String(64), nullable=True)
    acked_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Who acked (users.id; 0 when the token alone proved it and no identity
    # was carried). Kept consistent with acked_at by ck_..._acked_clocks.
    acked_by = Column(Integer, nullable=True)
    escalated_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # The topic the escalation re-page went to (oncall.owner_topic).
    escalated_to = Column(String(128), nullable=True)
    # 'sent' | 'acked' | 'escalated' | 'failed'.
    status = Column(String(16), nullable=False,
                    server_default=text("'sent'"), index=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('sent', 'acked', 'escalated', 'failed')",
            name="ck_oncall_pages_status"),
        # An ack always says when.
        CheckConstraint(
            "status != 'acked' OR acked_at IS NOT NULL",
            name="ck_oncall_pages_acked_clocks"),
        # An escalation always says when and where.
        CheckConstraint(
            "status != 'escalated' OR "
            "(escalated_at IS NOT NULL AND escalated_to IS NOT NULL)",
            name="ck_oncall_pages_escalated_clocks"),
        # A page that reached nobody has nothing to ack or escalate: only a
        # 'sent' row carries an ack token at all, and only a live ('sent')
        # row may still be acked/escalated — a terminal state is terminal.
        CheckConstraint(
            "ack_token_hash IS NULL OR status = 'sent'",
            name="ck_oncall_pages_token_only_live"),
        Index("ix_oncall_pages_status_sent", "status", "sent_at"),
        Index("ix_oncall_pages_dedupe_status", "dedupe_key", "status"),
    )


class SignupAgeAttestation(SoftDeleteMixin, Base):
    """GSEC FX.108 p1 (owner decision Q95=A) — the AGE ATTESTATION record.

    Q95=A: "hosted-cloud sign-up requires age 16+ (tick-box confirmation),
    under-13s never on the hosted cloud". So the hosted self-serve sign-up
    (POST /onboarding/create-new-tenant and /onboarding/finalize-invited-user)
    must carry an attestation, and the platform must be able to PROVE, after
    the fact, that an account on the hosted cloud was created with one —
    timestamped and audited. This table is that proof; the audit row is the
    compliance record (AU-2/AU-3), this row is the object-level record that
    ties the attestation to the account it authorized.

    NEW TABLE ONLY (models.py is bind-mounted live: no column was added to
    any existing table). SOFT-DELETE ONLY (ISO 27001 — no hard delete; and
    NO ON DELETE CASCADE / SET NULL on either FK: dropping a tenant or a
    user must never erase the record that its account was age-attested at
    creation).

    DATA MINIMISATION (the reason there is no birth date, no IP, no email
    here): the compliance question is "was this account 16+ when it was
    created", not "when was this person born". A birth year would be
    personal data the platform has no need for (and would put the install
    squarely under a child-data regime), so the row stores only the BAND
    the user ticked. A refused underage attempt writes NO row here at all
    (user_id is NOT NULL: an account that was never created has no
    attestation) and no audit metadata carries a name, email or birth
    year — only the band.

    ``attestation`` values:
        '16_plus'   the tick-box the hosted sign-up form shows ("I confirm
                    I am 16 or older") — the only value the hosted path
                    writes.
        '13_15'     reserved for the education/on-prem flow (FX.108 p3),
                    where a school admin attests a minor's account under
                    the school's own authority. The hosted sign-up path
                    NEVER writes it: a 13-15 self-sign-up is refused.
    """
    __tablename__ = "signup_age_attestations"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The account this attestation authorized. NOT NULL by design: a refused
    # underage attempt has no account, so it has no row.
    user_id = Column(ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    # No index=True here: the explicit ix_signup_age_attestations_attestation
    # below (band + timestamp, the shape the education profile sweeps) is the
    # only index on this column — an auto-named one would collide with it.
    attestation = Column(String(16), nullable=False,
                         server_default=text("'16_plus'"))
    # WHEN the user attested (the owner decision's "stored with timestamp").
    attested_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), index=True)

    __table_args__ = (
        CheckConstraint(
            "attestation IN ('16_plus', '13_15')",
            name="ck_signup_age_attestations_band"),
        # An attestation always says when (the timestamped requirement).
        CheckConstraint(
            "attested_at IS NOT NULL",
            name="ck_signup_age_attestations_clock"),
        Index("ix_signup_age_attestations_user_attested",
              "user_id", "attested_at"),
        Index("ix_signup_age_attestations_attestation",
              "attestation", "attested_at"),
    )


class TrialCardFingerprint(SoftDeleteMixin, Base):
    """FX.135 p2 (owner decision Q123=A) — ONE FREE TRIAL PER CARD.

    Q123=A: "Throwaway email domains blocked; one free trial per card or
    device." The card half of that rule lives here: Stripe hands us a stable
    per-card token (`payment_method.card.fingerprint` — same value for the
    same physical card across every customer), so a tenant signing up with a
    card another tenant already used for their free trial is refused.

    NEW TABLE ONLY (models.py is bind-mounted live and shared by every lane:
    no column was added to any existing table). SOFT-DELETE ONLY (ISO 27001
    — no hard delete; releasing a card soft-deletes the row and stamps
    `deleted_at`, so the record of "this card once owned a trial" survives).

    The DB enforces at most ONE LIVE row per card (`deleted_at IS NULL` in
    the partial unique index), so a soft-deleted (released) fingerprint never
    collides with a re-registration and the service's "another tenant already
    used this card" read is a single indexed lookup.

    DATA MINIMISATION: a fingerprint is an opaque Stripe token, not card data
    — no PAN, no last4, no brand, no customer name lives here.
    """
    __tablename__ = "trial_card_fingerprints"

    # BigInteger/Integer variant (same trick as AuditEvent) so the test
    # suite's engine autoincrements the key while Postgres keeps BIGSERIAL.
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # Stripe's stable per-card token (payment_method.card.fingerprint).
    # No column-level `unique=True` / `index=True`: uniqueness is the PARTIAL
    # unique index below (live rows only) and the plain lookup index is the
    # explicit one — an auto-named column index would collide with it.
    card_fingerprint = Column(String(64), nullable=False)
    # The tenant whose free trial this card owns. FK WITHOUT ON DELETE
    # CASCADE: deleting a tenant must never erase the record that its card
    # once claimed a trial (ISO 27001 A.18.1.3, protection of records).
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        # One LIVE claim per card; tombstones are excluded so a released
        # card can be re-registered without colliding with its own row.
        Index("uq_trial_card_fingerprints_live", "card_fingerprint",
              unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("ix_trial_card_fingerprints_tenant", "tenant_id"),
    )


class TrialDeviceFingerprint(SoftDeleteMixin, Base):
    """FX.135 p3b2 (owner decision Q123=A) — ONE FREE TRIAL PER DEVICE.

    Q123=A: "Throwaway email domains blocked; one free trial per card or
    device." FX.135 p2 built the CARD half (`TrialCardFingerprint`, Stripe's
    stable per-card token). This is the DEVICE half of the SAME rule: a tenant
    starting a free trial from a device another tenant already used for one is
    refused.

    There is no vendor token for a device (the trial-start routes carry only
    plan + days, and the login path records just the caller's client IP and
    user-agent), so the value stored here is a HASH the service derives from
    the two signals the request already carries — the client IP collapsed to
    its /24 network and the user-agent string. The RAW signals are never
    stored: no full IP, no user-agent verbatim, nothing device-specific beyond
    that coarse pair (data minimisation, same spirit as the card half, which
    stores an opaque Stripe token rather than card data).

    NEW TABLE ONLY (models.py is bind-mounted live and shared by every lane:
    no column was added to any existing table). SOFT-DELETE ONLY (ISO 27001 —
    releasing a device soft-deletes the row and stamps `deleted_at`, so the
    record "this device once owned a trial" survives; nothing is hard-deleted).

    The DB enforces at most ONE LIVE row per device fingerprint (`deleted_at
    IS NULL` in the partial unique index), so a soft-deleted (released)
    fingerprint never collides with a re-registration and the service's
    "another tenant already used this device" read is one indexed lookup.

    Index names use the `fp` short form so they stay under Postgres' 31-byte
    identifier limit (`trial_device_fingerprints` is one byte longer than its
    card sibling, and the card half's names sit exactly at the limit).
    """
    __tablename__ = "trial_device_fingerprints"

    # BigInteger/Integer variant (same trick as AuditEvent) so the test
    # suite's engine autoincrements the key while Postgres keeps BIGSERIAL.
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # sha-256 over (client IP /24, user-agent) — see the docstring. No column-
    # level `unique=True` / `index=True`: uniqueness is the PARTIAL unique
    # index below (live rows only) and the plain lookup index is the explicit
    # one — an auto-named column index would collide with it.
    device_fingerprint = Column(String(64), nullable=False)
    # The tenant whose free trial this device owns. FK WITHOUT ON DELETE
    # CASCADE: deleting a tenant must never erase the record that its device
    # once claimed a trial (ISO 27001 A.18.1.3, protection of records).
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        # One LIVE claim per device; tombstones are excluded so a released
        # device can be re-registered without colliding with its own row.
        Index("uq_trial_device_fp_live", "device_fingerprint",
              unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("ix_trial_device_fp_tenant", "tenant_id"),
    )


class ModelProvenance(Base):
    """FX.148 p4a (Q135=C) — provenance record for every model import.

    FX.148 says a model's provenance (source, revision, hash, licence,
    approver) must live IN THE INVENTORY, because "which build of which repo
    is this model, and did anything dangerous in it run" is the question a
    compliance reviewer asks while looking at the model list — and today the
    only answer is the model's own manifest, which records nothing about
    where it came from or what was scanned.

    APPEND-ONLY (ISO 27001 A.18.1.3, records shall not be destroyed): there
    is no `deleted_at`, no UPDATE, no soft delete. A re-import of the same
    model writes a NEW row; the newest row is the current provenance and
    every prior import stays readable. Nothing here is ever rewritten.

    NEW TABLE ONLY (models.py is bind-mounted live and shared by every lane:
    no column was added to any existing table).

    One row per import event:
      model_name/model_type  the inventory handle it was registered under
      source_url/revision    where the uploader SAID it came from (repo URL
                             + commit/tag). Declared, not verified — a
                             provenance record states what the importer
                             claimed; verification (cosign, FX.148 p4b) is
                             what later turns a claim into an attestation.
      file_sha256            {filename: sha256} of the staged artefact, so
                             "verify on every cache load" (FX.148) has the
                             expected digests to check against.
      scan_result            the FX.148 p3a pickle-scan verdict snapshot
                             (status + finding), so the inventory shows the
                             verdict that gated this import without re-scanning.
      imported_by_*          the actor (approver) and tenant scope.
    """
    __tablename__ = "model_provenance"

    # BigInteger().with_variant(Integer, "sqlite") so the test suite's
    # in-memory engine can autoincrement the primary key via ROWID
    # (same trick as AuditEvent / TrialCardFingerprint).
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    model_name = Column(String(200), nullable=False, index=True)
    # "embedding" | "llm" — the store's declared kind (ems.MODEL_TYPES).
    model_type = Column(String(16), nullable=False, index=True)
    # Declared source (repo URL / HF id / local path). Text, not String: a
    # provenance claim is free-form and must never be silently truncated.
    source_url = Column(Text, nullable=True)
    source_revision = Column(Text, nullable=True)
    # {filename: sha256} of the staged artefact (capped by
    # models.provenance_max_hash_files / models.provenance_max_file_bytes).
    file_sha256 = Column(JSONB, nullable=True)
    file_count = Column(Integer, nullable=False, default=0, server_default="0")
    total_bytes = Column(BigInteger, nullable=False, default=0,
                         server_default="0")
    # clean | unsafe | error | skipped | disabled — the pickle-scan verdict
    # that gated THIS import (never re-derived later).
    scan_status = Column(String(16), nullable=True, index=True)
    scan_result = Column(JSONB, nullable=True)
    # The actor (approver) and tenant scope. FK WITHOUT any ON DELETE clause:
    # deleting a user/tenant must never erase the record of what they once
    # imported (ISO 27001 A.18.1.3).
    imported_by_user_id = Column(ForeignKey("users.id"), nullable=True,
                                 index=True)
    imported_by_tenant_id = Column(ForeignKey("tenants.id"), nullable=True,
                                   index=True)
    # "upload" (embedding-model store) | "register" (named-artifact) — what
    # kind of import event this is, so the list can say how a model arrived.
    import_kind = Column(String(24), nullable=False, default="upload",
                         server_default="upload")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), index=True)

    __table_args__ = (
        Index("ix_model_provenance_name", "model_name", "model_type"),
        Index("ix_model_provenance_created", "created_at"),
    )


class FieldUploadSession(Base):
    """19.3.s2 — one resumable field-capture full upload (NEW TABLE ONLY).

    The field client pushes its first slice in seconds (19.3.s1) and the FULL
    capture data follows over the same poor link, which drops requests at
    random. A single big PUT dies there, so the device uploads chunks it can
    re-send and needs to know, after every interruption, which chunks made it.
    That resume state cannot live in memory (the API restarts, the device
    reboots) nor only in the bucket (the client must not have to list S3 to
    find its own session), so it lives here: one row per session, received
    chunks in ``parts``.

    NO HARD DELETE / NO TOMBSTONING: the row is a state machine
    (initiated -> completed | expired). An expired session's row stays so the
    field record of what a device attempted survives (ISO 27001 A.18.1.3);
    the chunk objects stay in the bucket too — nothing here destroys bytes.

    ``device_id`` is the DEVICE's public id (the token resolves it), not an FK
    row: a revoked device must still be able to finish a capture it started
    on site, and the first-slice record keys on the same string, so the two
    halves of a capture reconcile on it.
    """
    __tablename__ = "field_upload_sessions"

    # BigInteger().with_variant(Integer, "sqlite") so a non-Postgres test engine
    # can autoincrement the primary key via ROWID (same trick as AuditEvent).
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    device_id = Column(String(64), nullable=False, index=True)
    upload_id = Column(String(64), nullable=False, index=True)
    # The capture this upload belongs to (the first-slice record's key), so
    # complete() can find the s1 half and a re-init for the same capture
    # replaces the live session.
    capture_id = Column(String(128), nullable=True, index=True)
    # "initiated" | "completed" | "expired" — the resume state machine.
    state = Column(String(16), nullable=False, default="initiated",
                   server_default="initiated", index=True)
    total_size = Column(BigInteger, nullable=False)
    chunk_size = Column(Integer, nullable=False)
    # The digest the ASSEMBLED object must match (declared by the device at
    # init; verified at complete). Null = the device cannot pre-hash on site.
    expected_sha256 = Column(String(64), nullable=True)
    sha256 = Column(String(64), nullable=True)          # verified at complete
    object_key = Column(Text, nullable=True)            # final assembled object
    total_received_bytes = Column(BigInteger, nullable=False, default=0,
                                  server_default="0")
    # {"<chunk index>": {"sha256","size","object_key"}} of RECEIVED chunks —
    # the resume map the client reads back after an interruption.
    parts = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_field_upload_sessions_scope", "tenant_id", "device_id"),
    )


class DsrRequest(SoftDeleteMixin, Base):
    """FX.133 p2a (owner decision Q121=A) — a data-subject REQUEST (DSR).

    Q121=A says the rights-request flow supports the US-state request
    types, appeals and per-state deadlines (usually 45 days). A data
    subject (a customer of a controller) who wants something done about
    their personal data files a DSR: a TYPE, the SUBJECT it is about, an
    optional APPEAL against a decision, and a deadline clock
    (``received_at`` + the statutory-days platform setting) so the
    controller can prove it answered inside the statutory window.

    Request TYPES (the six GDPR/CCPA families; the state-law names map
    onto them — 'know' -> access/export, 'delete' -> erasure, 'correct'
    -> rectification, 'limit sensitive data' -> restriction):
        access_export   "show me what you hold about me" + export it
        rectification   "that record about me is wrong, fix it"
        erasure         "forget it" — SOFT-DELETE / ANONYMISE ONLY
        restriction     "stop processing me for this purpose"
        objection      a subprocessor objection (FX.141 family)
        portability     "move my data elsewhere" (export + hand-off)

    State machine (validated in services/dsr_requests.py, and the CHECK
    below keeps the DB honest):
        received -> verified -> in_progress -> completed
        received -> verified -> rejected
        received -> in_progress            (no separate verification step)
        received -> rejected               (refused on sight)
    Terminal states (completed/rejected) NEVER move again — an appeal
    does not reopen a DSR, it files an appeal row + an audit event
    against the same request, so a decision stays reviewable.

    ISO 27001 / no-hard-delete: ``erasure`` is the WORD the data subject
    uses; the platform NEVER hard-deletes anything on its account. An
    erasure DSR that completes marks the request ``anonymised`` (the
    subject's records are anonymised / soft-deleted elsewhere, their
    ``deleted_at`` stamped) — this row itself is never destroyed, and
    ``hard_delete`` is a CHECK-false constant: an erasure DSR that
    claims a hard delete is rejected by the DB.

    SOFT-DELETE ONLY: removal sets ``deleted_at``; the row stays as the
    compliance record of the request the subject made.

    NEW TABLE ONLY (models.py is bind-mounted live and shared by every
    lane: no column was added to any existing table).
    """
    __tablename__ = "dsr_requests"

    # BigInteger().with_variant(Integer, "sqlite") so the test suite's
    # in-memory engine can autoincrement the primary key via ROWID
    # (same trick as AuditEvent / TrialCardFingerprint).
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The tenant (controller) that RECEIVED this request — the data
    # subject's own account is scoped to it. FK with no ON DELETE clause:
    # deleting a tenant must never erase the record of what it was asked.
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False,
                       index=True)
    # The data subject's own account (users.id), when the request came in
    # through an authenticated session (NULL = submitted by other means,
    # e.g. a postal/written request handled by the controller).
    subject_user_id = Column(Integer, ForeignKey("users.id"), nullable=True,
                             index=True)
    # 'access_export' | 'rectification' | 'erasure' | 'restriction' |
    # 'objection' | 'portability'.
    request_type = Column(String(24), nullable=False, index=True)
    # What the request is about: the data-category / record handle the
    # controller's published table uses (free-form, never truncated).
    subject_ref = Column(String(255), nullable=False, index=True)
    # The subject's own words (required — a request with no stated reason
    # cannot be verified against anything).
    description = Column(Text, nullable=False)
    # 'received' | 'verified' | 'in_progress' | 'completed' | 'rejected'.
    status = Column(String(16), nullable=False, default="received",
                    server_default="received", index=True)
    # The statutory deadline: received_at + dsr.statutory_days (default
    # 45, the US-state statutory window Q121 names). Stamped at creation
    # so the deadline a reviewer reads is the deadline that was in force
    # WHEN THE REQUEST ARRIVED, even if the setting is retuned later.
    due_date = Column(DateTime(timezone=True), nullable=False, index=True)
    # When the controller verified the request actually concerns them
    # (identity proof), and when it was decided.
    verified_at = Column(DateTime(timezone=True), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    # The controller-side actor (users.id) that last moved the state.
    decided_by_user_id = Column(Integer, ForeignKey("users.id"),
                                nullable=True, index=True)
    # FX.133 p2a — the erasure OUTCOME marker: an erasure DSR that
    # completes marks the subject's records anonymised/soft-deleted
    # elsewhere. NEVER a hard delete (ISO 27001 A.18.1.3).
    anonymised = Column(Boolean, nullable=False, default=False,
                        server_default="false")
    # Hard-delete claim: always false. Present so the CHECK below can
    # state the rule in the DB, not just in the service.
    hard_delete = Column(Boolean, nullable=False, default=False,
                         server_default="false")
    # Appeal against a decision (owner Q121: appeals are part of the
    # flow). An appeal does not reopen the DSR.
    appeal_text = Column(Text, nullable=True)
    appeal_at = Column(DateTime(timezone=True), nullable=True)
    # The request this is a re-filed/extension of (e.g. a CCPA "extend"
    # window), NULL = first request for this subject_ref.
    parent_id = Column(Integer, ForeignKey("dsr_requests.id"), nullable=True,
                       index=True)
    # The statutory window in days actually used at creation (recorded so
    # a reviewer can re-derive due_date without re-reading the setting).
    statutory_days = Column(Integer, nullable=False, default=45,
                            server_default="45")
    # The state-law name the subject used, when they used one ('know' /
    # 'delete' / 'correct' / 'limit_sensitive'); the TYPE above is the
    # canonical family it maps to.
    state_law_label = Column(String(24), nullable=True, index=True)
    # The clock the request was received on (server_default keeps a
    # non-Python creation path honest; the service always stamps it).
    received_at = Column(DateTime(timezone=True), nullable=False,
                        index=True, server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    # SoftDeleteMixin adds `deleted_at` (nullable, indexed).

    __table_args__ = (
        CheckConstraint(
            "request_type IN ('access_export', 'rectification', 'erasure', "
            "'restriction', 'objection', 'portability')",
            name="ck_dsr_requests_type",
        ),
        CheckConstraint(
            "status IN ('received', 'verified', 'in_progress', "
            "'completed', 'rejected')",
            name="ck_dsr_requests_status",
        ),
        # ISO 27001 A.18.1.3: an erasure request is soft-delete/anonymise
        # ONLY. The DB refuses any DSR that claims a hard delete, so no
        # code path — present or future — can turn a subject's "forget
        # me" into destroyed records.
        CheckConstraint(
            "hard_delete = false",
            name="ck_dsr_requests_no_hard_delete",
        ),
        CheckConstraint(
            "statutory_days > 0 AND statutory_days <= 3650",
            name="ck_dsr_requests_statutory_days",
        ),
        # A decided row carries its decision clock; a verified row
        # carries its verification clock (the state machine cannot claim
        # a step without the timestamp that proves it).
        CheckConstraint(
            "status = 'received' OR status = 'verified' OR status = "
            "'in_progress' OR decided_at IS NOT NULL",
            name="ck_dsr_requests_decision_clocks",
        ),
        CheckConstraint(
            "status != 'verified' OR verified_at IS NOT NULL",
            name="ck_dsr_requests_verified_clock",
        ),
        Index("ix_dsr_requests_tenant_status", "tenant_id", "status"),
        Index("ix_dsr_requests_due", "status", "due_date"),
        Index("ix_dsr_requests_subject", "tenant_id", "subject_ref"),
    )


class ContractObligation(SoftDeleteMixin, Base):
    """FX.159 p3a (owner Q146=A) — the Enterprise CONTRACT-OBLIGATIONS
    tracker: what a customer's contract puts on MOD.

    docs/isms/requirements-register.md §3 names the five families the
    tracker holds per customer relationship:

        sla             the service-level terms in force (remediation
                        windows per docs/security-audit/REMEDIATION_SLA_POLICY.md)
        breach_notice   the per-customer notification deadline (72h default,
                        24h for gov/DoD profiles) + the security-contact path
        data_location   where that customer's data actually sits (hosted
                        region / residency / BYO-S3 destination)
        deletion_terms  the erasure promise (cooldown, legal-hold
                        interaction, what a deletion request does NOT destroy)
        contract_end    the dated end of the contract itself
        legal_hold      a dated legal-hold expiry (DFARS 90-day window)
        other           any other contractual term MOD owes a customer

    SCOPE: ``tenant_id`` NULL = a PLATFORM-level obligation (the on-prem
    customer-security-contact path, the house SLA); set = the obligation
    belongs to that customer relationship. Every read is scoped the same
    way the security-alert engine scopes its rows.

    OWNERSHIP: the register's "owner" column is a ROLE (security officer),
    so ``owner`` is the role name and ``owner_user_id`` is the account that
    holds it, when one exists.

    DUE DATES (the register's "every dated obligation carries a due date"):
    ``due_date`` NULL = a standing/undated obligation (a data-location
    fact); set = the date the obligation is due. ``recurrence`` drives the
    yearly-review rhythm the register asks for — a recurring obligation
    MUST be dated (CHECK below), and its next occurrence is recorded as a
    NEW row chained by ``parent_id`` so the last review stays reviewable
    (ISO 27001 AU-2/AU-3: never overwrite history).

    STATE: open -> acknowledged -> satisfied | breached; open -> satisfied
    | breached directly. satisfied/breached are terminal — a re-review
    files the NEXT occurrence, it never reopens a closed one.

    ISO 27001: SOFT DELETE ONLY (``deleted_at``); every create/update/
    close/withdraw writes an append-only AuditEvent row
    (contract_obligation.*). Nothing here destroys a row.

    NEW TABLE ONLY (models.py is bind-mounted live and shared by every
    lane: no column was added to any existing table).
    """
    __tablename__ = "contract_obligations"

    # BigInteger().with_variant(Integer, "sqlite") so the test suite's
    # in-memory engine can autoincrement the primary key via ROWID
    # (same trick as AuditEvent / DsrRequest).
    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The customer relationship this obligation belongs to; NULL = a
    # platform-level obligation. FK with NO ON DELETE clause: deleting a
    # tenant must never erase the record of what its contract asked of
    # MOD (ISO 27001 A.18.1.3).
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True,
                       index=True)
    # The contract/DPA/SLA this obligation comes from (free-form handle,
    # never truncated — 'DPA-2026-ACME', 'SLA-remediation-v2').
    contract_ref = Column(String(255), nullable=False, index=True)
    # 'sla' | 'breach_notice' | 'data_location' | 'deletion_terms' |
    # 'contract_end' | 'legal_hold' | 'other'.
    obligation_type = Column(String(24), nullable=False, index=True)
    # The obligation itself, in the customer's terms (required — an
    # obligation with no stated text cannot be satisfied or audited).
    obligation = Column(Text, nullable=False)
    # The register's owner column: a ROLE (default 'security_officer').
    owner = Column(String(64), nullable=False, default="security_officer",
                   server_default="security_officer", index=True)
    # The account that holds it, when one does.
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True,
                           index=True)
    # 'open' | 'acknowledged' | 'satisfied' | 'breached'.
    status = Column(String(16), nullable=False, default="open",
                    server_default="open", index=True)
    # 'none' | 'annually' | 'quarterly' | 'monthly' | 'weekly' | 'daily'
    # (the register's yearly review). A recurring obligation must be dated.
    recurrence = Column(String(16), nullable=False, default="none",
                        server_default="none", index=True)
    # The date this obligation is due; NULL = undated/standing.
    due_date = Column(DateTime(timezone=True), nullable=True, index=True)
    # The clocks that prove each step (the state machine cannot claim a
    # step without the timestamp that proves it).
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_by_user_id = Column(Integer, ForeignKey("users.id"),
                                     nullable=True)
    satisfied_at = Column(DateTime(timezone=True), nullable=True)
    satisfied_by_user_id = Column(Integer, ForeignKey("users.id"),
                                  nullable=True)
    breached_at = Column(DateTime(timezone=True), nullable=True)
    breached_by_user_id = Column(Integer, ForeignKey("users.id"),
                                 nullable=True)
    # The previous occurrence this one succeeds (recurrence chain), NULL
    # = first occurrence. FK with no ON DELETE: the chain stays readable.
    parent_id = Column(Integer, ForeignKey("contract_obligations.id"),
                       nullable=True, index=True)
    # The reviewer's evidence for this obligation (control map / policy
    # doc path, audit id, ...), free-form, never secrets.
    evidence_ref = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False, index=True)
    # SoftDeleteMixin adds `deleted_at` (nullable, indexed).

    __table_args__ = (
        CheckConstraint(
            "obligation_type IN ('sla', 'breach_notice', 'data_location', "
            "'deletion_terms', 'contract_end', 'legal_hold', 'other')",
            name="ck_contract_obligations_type",
        ),
        CheckConstraint(
            "status IN ('open', 'acknowledged', 'satisfied', 'breached')",
            name="ck_contract_obligations_status",
        ),
        CheckConstraint(
            "recurrence IN ('none', 'annually', 'quarterly', 'monthly', "
            "'weekly', 'daily')",
            name="ck_contract_obligations_recurrence",
        ),
        # A recurring obligation is a review rhythm — it cannot exist
        # without a date to review against.
        CheckConstraint(
            "recurrence = 'none' OR due_date IS NOT NULL",
            name="ck_contract_obligations_recurrence_needs_due",
        ),
        # A closed obligation carries the clock that closed it.
        CheckConstraint(
            "status = 'open' OR status = 'acknowledged' OR "
            "satisfied_at IS NOT NULL OR breached_at IS NOT NULL",
            name="ck_contract_obligations_close_clock",
        ),
        # An acknowledged obligation carries its acknowledgement clock.
        CheckConstraint(
            "status != 'acknowledged' OR acknowledged_at IS NOT NULL",
            name="ck_contract_obligations_ack_clock",
        ),
        Index("ix_contract_obligations_tenant_status", "tenant_id",
              "status"),
        Index("ix_contract_obligations_due", "status", "due_date"),
        Index("ix_contract_obligations_contract", "tenant_id",
              "contract_ref"),
    )


class AdminDevice(SoftDeleteMixin, Base):
    """FX.145 p2a — ONE NEW TABLE: an ADMIN/STAFF DEVICE in the asset inventory.

    FX.145 (owner decision Q132=A) says every admin/staff device that reaches
    an admin console is recorded in the FX.126 asset inventory (office-device
    kind), carries the security policy (FDE, auto OS updates, 5-min screen
    lock, no customer data local), and gets a yearly owner-confirmed
    self-check. FX.126's inventory is computed from the compute fleet
    (`worker_rows`/`manager_rows`/`stack_rows` over WorkerLicense/Manager/
    StackDeploy) — there is no type/kind column there and models.py is bind-
    mounted live and shared by every lane, so NO column was added to any
    existing table: this is the ONE new table that carries the office-device
    kind, and services/admin_device_records.py feeds it into
    `asset_inventory.build_inventory` as the `devices` rows (kind='device'),
    which is exactly the shape FX.145 p2b's yearly self-check sweep
    (services/device_self_check.py `_build_devices`) flattens.

    `tenant_id` is NULL for platform-scope devices (the platform-admin
    consoles' own workstations/phones belong to the platform, not a tenant)
    and set for a tenant's staff devices. FK WITHOUT ON DELETE CASCADE:
    deleting a tenant must never erase the record that its staff used this
    device (ISO 27001 A.18.1.3, protection of records).

    `owner_user_id` carries `ON DELETE SET NULL` (two words): losing the
    owning user must never erase the device record — the row is what the
    yearly self-check and the ingress/mesh-ACL gate (FX.145 p3a) read.

    SOFT DELETE ONLY (ISO 27001: NO hard delete anywhere): releasing a lost/
    stolen device stamps `archived_at` + `deleted_at` (SoftDeleteMixin) and
    keeps the row, so "this device once existed" survives in the trail.

    `id` is BIGSERIAL in the live DB (migration
    20261009b_fx145_p2a_admin_device_records.py); the sqlite variant keeps
    the test suite's engine autoincrementing the key (same trick as
    AuditEvent / MobileDevice).
    """
    __tablename__ = "admin_devices"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # NULL = platform-scope (platform-admin console devices). No ON DELETE
    # CASCADE — see the docstring.
    tenant_id = Column(ForeignKey("tenants.id"), nullable=True, index=True)
    # The staff/admin user this device belongs to (NULL = unassigned yet).
    owner_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"),
                           nullable=True, index=True)
    label = Column(String(128), nullable=False)
    device_type = Column(String(16), nullable=False)
    os = Column(String(64), nullable=True)
    # Hardware/serial identifier — free-form (dserial, IMEI, asset tag).
    serial_or_hw_id = Column(String(128), nullable=True)
    # Tailscale node id when the device is mesh-enrolled (FX.145 p3a gate).
    tailnet_node_id = Column(String(64), nullable=True, index=True)
    enrolled_at = Column(DateTime(timezone=True), nullable=False,
                         server_default=func.now())
    last_self_check_at = Column(DateTime(timezone=True), nullable=True,
                                index=True)
    status = Column(String(24), nullable=False, server_default="active")
    # Soft-delete tombstone (SoftDeleteMixin adds `deleted_at`, indexed);
    # `archived_at` is the human-readable archive clock + reason lives in
    # the audit row.
    archived_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "device_type IN ('workstation', 'phone', 'tablet')",
            name="ck_admin_devices_type",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'retired')",
            name="ck_admin_devices_status",
        ),
        # A device can only be archived once its status says it is out of
        # service (retired) — archiving an active device is a policy bug.
        CheckConstraint(
            "archived_at IS NULL OR status = 'retired'",
            name="ck_admin_devices_archive_needs_retired",
        ),
        Index("ix_admin_devices_tenant_status", "tenant_id", "status"),
        Index("ix_admin_devices_owner", "owner_user_id"),
    )


class WorkflowRunProofBundle(Base):
    """fortress-2.1b — ONE NEW TABLE: the SIGNED proof bundle for a run.

    fortress-2.1a (services/run_proof_bundle.py) builds and signs a run's
    PROOF: the canonical, byte-stable record of what one workflow run
    actually executed (per node run the exact image build, the artefacts it
    consumed/produced and their content hashes where the platform stores
    them), serialised canonically and signed with ONE platform key now.
    2.1b is the completion hook that STORES it: this table is where the
    signed bundle lives, so compliance auditors (and the client's own
    evidence chain) can retrieve a run's proof long after the run's
    containers, logs and cogs are gone.

    APPEND-ONLY BY DESIGN (ISO 27001 A.18.1.3, protection of records):
    there is NO `updated_at`, NO soft-delete tombstone and NO status flag —
    a proof is never edited, retracted or replaced in place. A later
    re-sign (key rotation, or fortress-2.4's second signature) is a NEW
    row, so the uniqueness that keeps "one proof per run" in the common
    case is `unique (workflow_run_id, key_id)`, NOT `unique (workflow_run_id)`:
    one proof per (run, signing key). A double-sign under the SAME key is
    refused (it would be a duplicate of the same evidence, and re-signing
    with the same key over the same bytes proves nothing new); a row under
    a DIFFERENT key is a genuinely different attestation and must coexist
    so the older one is never lost. `key_id` is therefore NOT NULL — a
    NULL would silently defeat the constraint (Postgres does not dedupe
    NULLs in a unique index).

    `workflow_run_id` FK carries NO ON DELETE clause: WorkflowRun is
    soft-deleted (SoftDeleteWithActorMixin), so nothing ever cascades —
    and hard-deleting a run must never erase the proof of what it did.

    `id` is BIGSERIAL in the live DB (migration
    20261009c_fortress_21b_workflow_run_proof_bundles.py); the sqlite
    variant keeps the test suite's engine autoincrementing the key (same
    trick as AuditEvent / SecurityAlert / AdminDevice).
    """
    __tablename__ = "workflow_run_proof_bundles"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # The run's tenant (WorkflowRun.tenant_id is NOT NULL, so neither is
    # this). Indexed: the compliance sweeps read one tenant's proofs.
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    # The run this is the proof OF. Indexed; unique per (run, key) below.
    workflow_run_id = Column(ForeignKey("workflow_runs.id"),
                             nullable=False, index=True)
    # Bundle schema tag, e.g. "mod-run-proof-bundle/1.0" — the shape the
    # bytes in `bundle` follow, so a future schema bump coexists.
    schema = Column(String(64), nullable=False)
    # The signed bundle itself (the `proof` block is inside it, exactly as
    # sign_bundle returns it). NEVER a content hash of a secret: the
    # builder records nulls for digests the platform never stamped.
    bundle = Column(JSONB, nullable=False)
    # sha256 of the canonical signed message — lets a verifier localise
    # tampering cheaply before touching the signature. Indexed: "is this
    # exact proof already on file?" is a lookup auditors make.
    canonical_sha256 = Column(String(64), nullable=False, index=True)
    # Signing algorithm, e.g. "ed25519" (services/output_signature.ALGORITHM).
    alg = Column(String(32), nullable=False)
    # Public handle of the key that signed (platform-settings registry
    # `output.signature_key_id`). NOT NULL — see the docstring.
    key_id = Column(String(128), nullable=False, index=True)
    # The signature, base64 (Ed25519). Key MATERIAL is never stored here;
    # only the key's public handle above.
    signature_b64 = Column(Text, nullable=False)
    # The bundle's own honesty flag: False = the proof says it has gaps
    # (a digest the platform never stamped). Stored so a sweep can find
    # incomplete proofs without parsing every bundle.
    complete = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), index=True)

    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id", "key_id",
            name="uk_workflow_run_proof_bundles_run_key",
        ),
        Index("ix_workflow_run_proof_bundles_tenant_created",
              "tenant_id", "created_at"),
        Index("ix_workflow_run_proof_bundles_incomplete",
              "workflow_run_id", "complete"),
    )


class OutputSafetyScanResult(Base):
    """Fortress 3.4 — output_safety_scan_results (append-only scan record).

    One row per safety scan of a workflow OUTPUT (the S3 key/handle, never
    content). verdict pass|warn|block|error. An override is a NEW row that
    keeps the verdict and sets overridden_by_user_id + override_reason —
    never an UPDATE of the scan row. No updated_at/deleted_at: the DB
    migration pairs this table with the audit_append_only_guard trigger
    pair, so the model must never gain mutation columns (ISO 27001).
    """

    __tablename__ = "output_safety_scan_results"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # Scanning tenant scope. Indexed: sweeps read one tenant's scan history.
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    # The run whose output was scanned (workflow_runs.id). Nullable:
    # platform-scope scans (e.g. storage sweeps) may not tie to a run.
    workflow_run_id = Column(ForeignKey("workflow_runs.id"), nullable=True,
                             index=True)
    # The node run that produced the output (node_runs.id). Nullable.
    node_run_id = Column(ForeignKey("node_runs.id"), nullable=True, index=True)
    # The S3 key/handle of the scanned output — NEVER the content itself.
    output_ref = Column(String(512), nullable=False)
    # Content type of the scanned output (informational).
    mime = Column(String(128), nullable=True)
    # Which scanner ran (policy register lives in services/).
    scanner = Column(String(64), nullable=False)
    # pass | warn | block | error (CHECK below).
    verdict = Column(String(16), nullable=False)
    # Masked findings only — never raw offending content (nullable=False so
    # a scan row is always valid JSONB).
    findings = Column(JSONB, nullable=False, server_default="[]")
    # Set on an OVERRIDE row (a NEW row, verdict kept): the user who
    # overrode the scan verdict. NULL on plain scan rows.
    overridden_by_user_id = Column(ForeignKey("users.id"), nullable=True,
                                   index=True)
    # Free-text justification on an override row.
    override_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), index=True)

    __table_args__ = (
        CheckConstraint(
            "verdict IN ('pass', 'warn', 'block', 'error')",
            name="ck_output_safety_scan_results_verdict",
        ),
        Index("ix_output_safety_scan_results_tenant_verdict_created",
              "tenant_id", "verdict", "created_at"),
    )


class WizardRationale(SoftDeleteMixin, Base):
    """workflow-expressiveness-primitives §5.6 — `wizard_rationale` table.

    workflow-wizard-self-explanation §5.5 — one row per
    decision point the wizard captured during a compose turn: what the
    agent had to choose, what it chose, and why. The collector
    (services.wizard_rationale.record_rationale) writes these
    throughout the agentic loop; the final wizard response ships them
    as the `rationale` field ([{decision_point, chosen_option,
    alternatives_considered, reason}]) and the panel renders them under
    "Why this workflow?".

    Persistence-only here: nothing in the wizard pipeline calls the
    collector yet (wiring is a later lead step).

    `step` is the decision-point label (topology / node-selection /
    config), `rationale` is the human-readable reason. The structured
    alternatives live in the response payload; this row is the durable
    per-decision record.

    Soft-deleted (ISO 27001: no hard deletes) — a rationale row is
    evidence, never un-recorded. FK refs use ON DELETE SET NULL so the
    row survives session/build pruning.
    """
    __tablename__ = "wizard_rationale"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # Scoping tenant. NOT NULL + indexed: every read is one tenant's
    # history; cross-tenant leakage of design reasoning is the leak
    # this table exists to prevent.
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    # Which user's wizard turn produced this decision. Nullable:
    # platform/system-initiated builds have no acting user.
    user_id = Column(ForeignKey("users.id", ondelete="SET NULL"),
                     nullable=True, index=True)
    # The wizard conversation this decision came from
    # (support_chat_sessions.id). Nullable: node-wizard builds have no
    # chat session. SET NULL keeps the evidence row when the session is
    # pruned.
    wizard_session_id = Column(String(36), ForeignKey("support_chat_sessions.id",
                                                      ondelete="SET NULL"),
                               nullable=True, index=True)
    # The node build this decision came from (build_specs.id). Nullable.
    build_spec_id = Column(ForeignKey("build_specs.id",
                                      ondelete="SET NULL"),
                           nullable=True, index=True)
    # Decision-point label, e.g. 'image-upscale-node-choice' or
    # 'edge-A-to-B-port-type-bridge'. NOT NULL: a rationale row without
    # a decision point is meaningless.
    step = Column(String(64), nullable=False)
    # Human-readable reason for the choice (spec's `reason`). NOT NULL:
    # an empty rationale row is a UI bug, not a valid record.
    rationale = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), index=True)

    __table_args__ = (
        # Panel/response read: one tenant's decisions in build order.
        Index("ix_wizard_rationale_tenant_created",
              "tenant_id", "created_at"),
        # "Why this workflow?" read: one tenant's decisions for one
        # wizard conversation.
        Index("ix_wizard_rationale_tenant_session",
              "tenant_id", "wizard_session_id"),
    )


class RunPriceQuote(Base):
    """sz-tcp.22 (tiered-credit-pricing §9.3, owner decision 10-10) —
    `run_price_quotes`: the LOCKED quote a user saw before clicking Run.

    Owner decision 10-10: users are charged the quote shown before Run,
    never a re-estimate after the fact (prediction drift, mid-run price
    changes, etc. are MOD's risk to absorb, not the customer's). This
    table is where that locked number lives, one row per run.

    `breakdown` is per-node: [{node_id, credits, pricing_source,
    predicted_runtime_s}] — enough detail to explain "why does this run
    cost X credits" and to let the sz-tcp.24 audit/refit job compare the
    locked number against actual spend without recomputing it.

    NOT wired into the run-creation or credit-debit path yet (separate
    task, sz-tcp.22 row names credit_manager.py as the charge-side
    follow-up) -- `lock_quote`/`get_locked_quote` below are the
    persistence primitives only.

    No hard deletes (ISO 27001): a locked quote is the record of what the
    tenant was promised, kept forever once written.
    """
    __tablename__ = "run_price_quotes"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # One quote per run, ever — lock_quote() is idempotent on this.
    run_id = Column(ForeignKey("workflow_runs.id"), nullable=False,
                    unique=True, index=True)
    # Every read is tenant-filtered (get_locked_quote); indexed for that
    # and for future "all quotes for tenant X" admin/audit reads.
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    quoted_credits = Column(Integer, nullable=False)
    # Per-node [{node_id, credits, pricing_source, predicted_runtime_s}].
    breakdown = Column(JSONB, nullable=False,
                       server_default=text("'[]'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), index=True)


class WorkflowMcpExposure(Base):
    """sz-mcp-per-workflow.1 (mcp-fleet §per-workflow) —
    `workflow_mcp_exposure`: which workflows are exposed as MCP tools.

    One row per (workflow, tool_name) exposure. `Workflow.publish_mcp` is
    the intent switch on the workflow itself; this table is the record of
    the actual MCP-tool registration (tool_name per tenant, enabled
    toggle, timestamps). Router wiring (list/expose endpoints) is
    sz-mcp-per-workflow.2/3 — NOT here.

    FKs use ON DELETE SET NULL (two words): an exposure row survives its
    workflow/tenant row being soft-pruned; the row is the audit-side
    record of what was published, and ISO 27001 says no hard deletes.
    `id` is BIGSERIAL PRIMARY KEY.
    """
    __tablename__ = "workflow_mcp_exposure"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="SET NULL"),
                         nullable=True, index=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="SET NULL"),
                       nullable=True, index=True)
    tool_name = Column(String(160), nullable=False)
    enabled = Column(Boolean, default=True, nullable=False,
                     server_default='t')
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("workflow_id", "tool_name",
                         name="uq_workflow_mcp_exposure_tool"),
        Index("ix_workflow_mcp_exposure_tenant", "tenant_id", "tool_name"),
    )


class WorkflowParameterVersion(SoftDeleteMixin, Base):
    """`workflow_parameter_versions`: a
    snapshot of a differentiable workflow's parameters, written either by a
    training step (version_id/training_step_id, the original
    shape), or keyed by the newer spec columns (workflow_version_id,
    training_step_run_id, version_number, source, parent_version_id,
    created_by) added by migration 20261012n so every training step
    SHALL persist a new versioned parameter set, the tenant SHALL be able
    to roll back to any prior version, and non-training runs SHALL use the
    latest version.

    SOFT-DELETE ONLY (ISO 27001) for the new columns' FKs: none of
    training_step_run_id / parent_version_id / created_by carries
    `ondelete="CASCADE"` -- all are `ondelete="SET NULL"` so parameter
    history survives its run / parent set / user going away. `workflow_id`
    keeps its original `ondelete="CASCADE"` (migration 20261012n's ALTER
    branch does not touch it).
    """
    __tablename__ = "workflow_parameter_versions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    version_id = Column(UUID(as_uuid=True), nullable=False,
                        default=uuid.uuid4)
    parameters = Column(JSONB, nullable=False)
    training_step_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())

    # -- expr-10.4 (migration 20261012n) additions --
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    workflow_version_id = Column(ForeignKey("workflow_versions.id"),
                                 nullable=False, index=True)
    training_step_run_id = Column(ForeignKey("workflow_runs.id",
                                             ondelete="SET NULL"),
                                  nullable=True, index=True)
    version_number = Column(Integer, nullable=False, index=True)
    parent_version_id = Column(ForeignKey("workflow_parameter_versions.id",
                                          ondelete="SET NULL"),
                               nullable=True)
    source = Column(String(16), nullable=False)
    created_by = Column(ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)
    # deleted_at comes from SoftDeleteMixin -- do not redeclare it here.

    __table_args__ = (
        Index("ix_workflow_parameter_versions_workflow_created",
              "workflow_id", created_at.desc()),
        UniqueConstraint("workflow_version_id", "version_number",
                         name="uq_workflow_parameter_versions_version_number"),
        CheckConstraint("source IN ('training_step', 'rollback', 'manual')",
                        name="ck_workflow_parameter_versions_source"),
        Index("uq_workflow_parameter_versions_step", "workflow_version_id",
              "training_step_run_id", unique=True,
              postgresql_where=text("training_step_run_id IS NOT NULL")),
        Index("ix_workflow_parameter_versions_tenant_workflow_created",
              "tenant_id", "workflow_id", text("created_at DESC")),
    )


class WorkflowBudget(Base):
    """sz-1.2.1 (sz-productivity-floor decomp §1.2, model + migration
    only) — `workflow_budgets`: per-workflow max_cost_per_run +
    per-tenant max_cost_per_period caps.

    A row with `workflow_id` NULL is the tenant-wide default budget; a
    row with `workflow_id` set overrides the default for that one
    workflow. At most one of each per tenant, enforced by the
    (tenant_id, workflow_id) unique constraint below (NULLs are distinct
    in Postgres, so exactly one default row plus one row per overridden
    workflow is allowed).

    This migration only creates storage; no enforcement/reader code is
    wired here — a later decomp row reads these caps at run-creation /
    quote time to reject or warn on over-budget runs.

    `workflow_id` uses ON DELETE CASCADE (two words): a budget row tied
    to one specific workflow has no meaning once that workflow is
    hard-deleted. `tenant_id` also cascades — the budget row belongs to
    the tenant.
    """
    __tablename__ = "workflow_budgets"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="CASCADE"),
                         nullable=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    max_cost_per_run_credits = Column(Integer, nullable=True)
    max_cost_per_period_credits = Column(Integer, nullable=True)
    period_window = Column(String(20), nullable=False,
                           server_default="monthly")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "workflow_id",
                         name="uq_workflow_budgets_tenant_workflow"),
        Index("ix_workflow_budgets_tenant", "tenant_id"),
    )


class PersistentStackBackupConfig(Base):
    """sz-nice-to-haves.4 (sz-nice-to-haves decomp, model + migration
    only) — `persistent_stack_backup_config`: Service Wizard backup
    posture per persistent stack (logical-dump vs raw-volume vs none),
    with a `requires_ack` flag for destructive/lossy postures.

    One row per `persistent_stacks` row (unique constraint below); the
    row is created lazily by a later task when a stack's backup posture
    is first set — this migration only creates storage, no
    `services/service_backup.py` writer/reader logic is wired here.

    No secret values live on this table. If a future backup target
    needs credentials, those go in Vault and this table would hold a
    path reference only (matching `PersistentStack.secret_refs` /
    stack-secrets-and-certs §1), never a plaintext key/secret column.

    `persistent_stack_id` uses ON DELETE CASCADE (two words): a backup
    posture row has no meaning once its stack is hard-deleted.
    `acked_by_user_id` uses ON DELETE SET NULL (two words): losing the
    acking user shouldn't delete the ack record, just its attribution.
    """
    __tablename__ = "persistent_stack_backup_config"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    persistent_stack_id = Column(
        ForeignKey("persistent_stacks.id", ondelete="CASCADE"),
        nullable=False)
    # logical_dump | raw_volume | none
    backup_mode = Column(String(20), nullable=False,
                         server_default="logical_dump")
    requires_ack = Column(Boolean, nullable=False, server_default="false")
    acked_by_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"),
                              nullable=True)
    acked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "backup_mode IN ('logical_dump', 'raw_volume', 'none')",
            name="ck_persistent_stack_backup_config_mode",
        ),
        UniqueConstraint("persistent_stack_id",
                         name="uq_persistent_stack_backup_config_stack"),
    )


class ProductionOutputFingerprint(Base):
    """sz-self-improving.1a (sz-self-improving decomp §1.1, model +
    migration only) — `production_output_fingerprints`: one row per
    sampled production-output fingerprint, the drift-surveillance input
    described in `the internal design notes
    tasks.md` §1.1 and design.md Decision 3.

    `fingerprint_kind` is free-text (not an enum/CHECK): the extractor
    in `services/drift_fingerprint.py` (`_EXTRACTORS`, already committed
    in 52e698fd6) owns valid kinds (`image_embedding`, `text_embedding`,
    `tensor_stats`, `schema_hash`, `binary_hash`, ...); this migration
    only creates storage, no extractor/writer/reader code is wired here
    (that is sz-self-improving.4/.5/.6, separate later tasks).

    `fingerprint_payload` is the extractor's full return dict (vector /
    stats / hash + a `degenerate` flag). `degenerate` is a denormalized
    top-level copy of `fingerprint_payload["degenerate"]` so a drift
    sweep can filter/index without parsing JSONB -- matches the
    reviewer note on 52e698fd6 that a degenerate fingerprint must never
    silently count as real signal.

    `workflow_id` / `workflow_run_id` / `node_run_id` all use ON DELETE
    SET NULL (two words): a fingerprint for a since-deleted workflow,
    run, or node run is still useful drift history, so the row outlives
    its parent, matching the no-hard-delete ISO posture.
    """
    __tablename__ = "production_output_fingerprints"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="SET NULL"),
                         nullable=True)
    workflow_run_id = Column(ForeignKey("workflow_runs.id", ondelete="SET NULL"),
                             nullable=True)
    node_run_id = Column(ForeignKey("node_runs.id", ondelete="SET NULL"),
                         nullable=True)
    # image_embedding | text_embedding | tensor_stats | schema_hash |
    # binary_hash | ... (owned by services/drift_fingerprint.py extractors)
    fingerprint_kind = Column(String(40), nullable=False)
    fingerprint_payload = Column(JSONB, nullable=False)
    degenerate = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())

    __table_args__ = (
        Index(
            "ix_production_output_fingerprints_workflow_kind_created",
            "workflow_id", "fingerprint_kind", "created_at",
        ),
        Index(
            "ix_production_output_fingerprints_tenant",
            "tenant_id",
        ),
    )


class WorkerHostExecJob(Base):
    """hx.1 (the internal design notes, decomp D1-D4) --
    `worker_host_exec_jobs`: one row per host-exec request dispatched to
    a worker's host-helper socket.

    D1: containerized workers run a host helper on a unix socket that
    accepts only jobs the API has signed for that worker (systemd-
    installed workers exec directly, no helper needed); this row is the
    signed job record the helper checks against.
    D2: callers are "any user who owns or has been granted the target
    worker"; shared/platform workers require platform-admin. That
    authorization check happens in the API layer that inserts rows here,
    not in this table.
    D3: no command allowlist -- `argv_redacted` holds the secret-redacted
    argv for audit/display, `argv_sha256` is the integrity hash of the
    real argv sent to the worker.
    D4: `run_as` has no DB-level default because "the worker's service
    user" is worker-specific, resolved by the API before insert;
    `run_as='root'` is explicitly allowed (D4), not specially flagged.

    Append-only (ISO 27001): no delete path is provided here or in the
    migration; jobs age out via retention tooling, never a DELETE.
    """
    __tablename__ = "worker_host_exec_jobs"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer, ForeignKey("tenants.id"),
        nullable=True,
    )  # nullable: platform workers have no owning tenant
    worker_id = Column(
        Integer, ForeignKey("worker_licenses.id"),
        nullable=False,
    )
    actor_user_id = Column(
        Integer, ForeignKey("users.id"),
        nullable=True,
    )
    argv_sha256 = Column(String(64), nullable=False)
    argv_redacted = Column(PostgresqlJSONB, nullable=False)
    cwd = Column(Text, nullable=True)
    run_as = Column(String(64), nullable=False)
    timeout_s = Column(Integer, nullable=True)
    status = Column(String(16), nullable=False, default="queued",
                    server_default="queued")
    exit_code = Column(Integer, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    output_object_key = Column(Text, nullable=True)
    output_truncated = Column(Boolean, nullable=False, default=False,
                              server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed',"
            "'timeout','cancelled')",
            name="ck_worker_host_exec_jobs_status",
        ),
        Index(
            "ix_worker_host_exec_jobs_worker_created",
            "worker_id", "created_at",
        ),
        Index(
            "ix_worker_host_exec_jobs_actor",
            "actor_user_id",
        ),
    )


class WorkerHostExecPolicy(Base):
    """hx.15 (worker-host-exec decomp, D2): per-worker access policy --
    who may submit host-exec jobs against a given worker.

    `access` is one of:
        'admins'       -- only platform/tenant admins (default, most
                          restrictive).
        'grants'       -- admins plus any user with an active (non-
                          revoked) `WorkerHostExecGrant` row for this
                          worker.
        'all_members'  -- any member of the worker's owning tenant
                          (platform workers: any authenticated user).

    Soft-delete via `deleted_at` (ISO 27001: no hard delete); the
    partial unique index on `worker_id` only considers non-deleted
    rows, so a worker can get a fresh policy row after a prior one is
    soft-deleted.

    Plain FKs (`worker_id` -> worker_licenses, `updated_by_user_id` ->
    users), no ON DELETE action: this is an access-control audit
    record and must never be anonymised or cascaded away.

    Not wired into `services/worker_host_exec_service.py` by this task
    -- that stub (`_worker_access_policy`) is owned by a separate hx.5
    agent; see LEAD STEPS in the hx.15 task brief for the follow-up
    wiring change.
    """
    __tablename__ = "worker_host_exec_policies"

    id = Column(Integer, primary_key=True)
    worker_id = Column(
        Integer, ForeignKey("worker_licenses.id"),
        nullable=False,
    )
    access = Column(String(16), nullable=False, default="admins",
                    server_default="admins")
    updated_by_user_id = Column(
        Integer, ForeignKey("users.id"),
        nullable=True,
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "access IN ('admins','grants','all_members')",
            name="ck_worker_host_exec_policies_access",
        ),
        Index(
            "ux_worker_host_exec_policies_worker",
            "worker_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class WorkerHostExecGrant(Base):
    """hx.15 (worker-host-exec decomp, D2): explicit per-user grant of
    host-exec access to a worker, used when the worker's
    `WorkerHostExecPolicy.access == 'grants'`.

    Revoke = set `revoked_at` (+ `revoked_by_user_id`); rows are never
    deleted (ISO 27001 append-only audit trail of who was granted/
    revoked access and when). The partial unique index on
    `(worker_id, grantee_user_id)` only covers non-revoked rows, so a
    previously-revoked grantee can be re-granted later without
    violating uniqueness.

    `allow_root` mirrors hx.1's `run_as='root'` allowance (D4): a grant
    can explicitly permit root-level host-exec jobs for that grantee on
    that worker.

    Plain FKs (`worker_id` -> worker_licenses, `grantee_user_id` /
    `granted_by_user_id` / `revoked_by_user_id` -> users), no ON DELETE
    action -- audit trail integrity over referential convenience.
    """
    __tablename__ = "worker_host_exec_grants"

    id = Column(Integer, primary_key=True)
    worker_id = Column(
        Integer, ForeignKey("worker_licenses.id"),
        nullable=False,
    )
    grantee_user_id = Column(
        Integer, ForeignKey("users.id"),
        nullable=False,
    )
    granted_by_user_id = Column(
        Integer, ForeignKey("users.id"),
        nullable=False,
    )
    allow_root = Column(Boolean, nullable=False, default=False,
                        server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id = Column(
        Integer, ForeignKey("users.id"),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ux_worker_host_exec_grants_worker_grantee",
            "worker_id", "grantee_user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class NodeCostBenchmark(Base):
    """sz-1.1.1 (sz-productivity-floor §1.1) — `node_cost_benchmarks`:
    observed cost + runtime stats per (node definition, size bucket).

    The table already exists live (migration `20261010c_node_cost_benchmarks`,
    applied); this class is the ORM view of it so the sz-1.4.1 cost forecaster
    can read per-node/per-bucket benchmarks without raw SQL. No writer is wired
    here yet (that is a later sz-1.4.1 row) -- this is a PURE ADD describing
    exactly the columns the migration created.

    One row per (node_definition_id, size_bucket), updated in place as more
    samples arrive (`sample_count` grows, p50/p95 re-computed). `size_bucket`
    is free-text format-family naming ("4MP", "32MP", "300s", ...) owned by
    the forecaster, not an enum.

    The node FK is ON DELETE SET NULL: a benchmark row for a node definition
    that has since been soft-pruned is still useful historical data (ISO 27001
    no-hard-delete posture); it just stops pointing at a live row. Hence the
    FK is nullable and uniqueness is a plain unique constraint, not a partial
    index -- a NULL node id still occupies its bucket slot.
    """
    __tablename__ = "node_cost_benchmarks"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    node_definition_id = Column(
        ForeignKey("node_definitions.id", ondelete="SET NULL"),
        nullable=True)
    size_bucket = Column(String(40), nullable=False)
    sample_count = Column(Integer, nullable=False, default=0,
                          server_default="0")
    p50_runtime_ms = Column(Integer, nullable=True)
    p95_runtime_ms = Column(Integer, nullable=True)
    p50_cost_credits = Column(Integer, nullable=True)
    p95_cost_credits = Column(Integer, nullable=True)
    last_observed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())

    __table_args__ = (
        UniqueConstraint("node_definition_id", "size_bucket",
                         name="uq_node_cost_benchmarks_node_bucket"),
        Index("ix_node_cost_benchmarks_node", "node_definition_id"),
    )


class CostRoutingDecision(Base):
    """sz-1.3.1 (sz-productivity-floor decomp §1.3, model + migration
    only) — `cost_routing_decisions`: the append-only audit of every
    cost-based VARIANT ROUTING decision a node made while running
    (which accelerator/image variant the scheduler picked out of the
    eligible candidates, and what it cost-forecasted for each).

    ISO 27001 append-only: rows are NEVER updated or deleted, so there
    is no soft-delete column and no updated_at — the table is pure
    history. `workflow_run_id` / `node_definition_id` use ON DELETE
    SET NULL (two words): a decision recorded for a since-deleted run
    or node definition is still useful routing history, so the row
    outlives its parent; `tenant_id` has NO ondelete action (matching
    the other audit tables in this file — WorkflowOutputAuditLog,
    AuditEvent, PermissionAuditLog, PlatformSettingAudit — none of
    which cascade on tenant delete): a tenant row cannot be
    hard-deleted out from under its own audit trail.

    `candidates` is the JSONB list of what the router considered, each
    item {variant, est_cost_credits, est_latency_s, cache_hit_p,
    eligible, reason}; `chosen_variant` is the one picked (e.g.
    "cuda"/"rocm"/"cpu" or an image tag). `budget_cap_credits` echoes
    the applicable workflow_budgets cap at decision time (NULL = no
    cap), `forecast_credits` the winning candidate's forecast.

    No writer/reader code is wired here — a later decomp row records
    these at variant-selection time and a later one reads them for
    routing-feedback / cost-surveillance sweeps.
    """
    __tablename__ = "cost_routing_decisions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False)
    workflow_run_id = Column(ForeignKey("workflow_runs.id", ondelete="SET NULL"),
                             nullable=True)
    node_id = Column(String(128), nullable=True)
    node_definition_id = Column(ForeignKey("node_definitions.id",
                                           ondelete="SET NULL"),
                                nullable=True)
    chosen_variant = Column(String(64), nullable=False)
    candidates = Column(JSONB, nullable=False, default=list,
                        server_default=text("'[]'::jsonb"))
    reason = Column(String(256), nullable=False)
    budget_cap_credits = Column(Numeric(12, 2), nullable=True)
    forecast_credits = Column(Numeric(12, 2), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())

    __table_args__ = (
        Index("ix_cost_routing_decisions_tenant", "tenant_id"),
        Index("ix_cost_routing_decisions_run", "workflow_run_id"),
        Index("ix_cost_routing_decisions_created", "created_at"),
    )


class WorkflowRunDispatchPriority(Base):
    """sz-1.9.1 (sz-productivity-floor decomp §1.9, owner Q-SZ1.9 = A) —
    `workflow_run_dispatch_priority`: the COST/CAPACITY DISPATCH TIER a
    run was dispatched at, kept in a NEW 1:1 side table instead of a new
    column on `workflow_runs`.

    Why a side table (Q-SZ1.9 = A): `workflow_runs` is the hottest table
    in the platform (every node/task/audit row FKs it); adding a column
    there means an ALTER on a huge table at deploy time. A 1:1 side
    table is created cheaply and read only by the dispatcher.

    This is NOT `workflow_runs.priority_class` (models.py ~2396), which
    is a SCHEDULING class (normal/high/low/interactive) that drives the
    packer's placement ALGORITHM (HEFT + 2-opt vs the warmth-score
    lightweight path). `dispatch_priority` is orthogonal: the COST/CAPACITY
    tier the run asked to be served from — `on_demand` (default: full-price
    worker, guaranteed capacity) or `spot` (the 5-10x cheaper spot /
    preemptible pool per workflow-cost-economics §"spot / preemptible
    worker pool"; spot runs are killed and re-queued when capacity is
    reclaimed). A run can be `priority_class=high` AND `dispatch_priority=spot`.

    ABSENCE IS NOT NULL: a run with NO row here means `on_demand`. The
    server_default exists so an explicitly-written row is never NULL, but
    the dispatcher MUST treat "no row" as on_demand — it never has to
    join for the common case.

    Soft-delete platform (ISO 27001, no hard deletes): `workflow_run_id`
    carries NO ondelete action (matching the other 1:1 run side tables in
    this file — RunPriceQuote, WorkflowMcpExposure, RunProofBundle — none
    of which cascade), and `tenant_id` likewise has no cascade: a tenant
    row cannot be hard-deleted out from under its own dispatch record.
    `updated_at` exists because the tier is mutable (an admin can
    re-dispatch a stuck run onto spot), unlike the append-only audit
    tables.
    """
    __tablename__ = "workflow_run_dispatch_priority"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # One row per run, ever — the dispatcher's set-once/overwrite entry
    # point is idempotent on this UNIQUE.
    workflow_run_id = Column(ForeignKey("workflow_runs.id"), nullable=False,
                             unique=True, index=True)
    # Every read is tenant-filtered; indexed for that and for
    # "all spot runs for tenant X" cost-surveillance sweeps.
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False, index=True)
    dispatch_priority = Column(String(16), nullable=False, default="on_demand",
                               server_default=text("'on_demand'"))
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("dispatch_priority IN ('on_demand', 'spot')",
                        name="ck_workflow_run_dispatch_priority_value"),
        Index("ix_workflow_run_dispatch_priority_tenant", "tenant_id"),
    )

class OutputDeliveryLog(Base):
    """sz-4.7.1 (sz-productivity-floor decomp §4.7.1, model + migration
    only) — `output_delivery_log`: the append-only audit of every OUTPUT
    DELIVERY the platform attempted (the Slack / Discord / email / SMS /
    push / webhook / file output nodes, decomp §4.7 and the container-side
    nodes 154/160).

    One row per delivery ATTEMPT, written by a future API-side logger when
    an output node finishes: what channel was used, where it went, whether
    it landed, what the remote endpoint answered, and which attempt this
    was. Rows are never updated or deleted — no soft-delete column, no
    updated_at; retention is a soft policy (rows are archived/exported,
    never hard-deleted), per ISO 27001 no-hard-delete.

    WHAT "REDACTED" MEANS here: nothing in this table may hold the payload
    or the destination itself.

    * `destination_hint` is the REDACTED destination — the host / channel
      name only (e.g. "hooks.slack.com", "#mods-general", "alerts@acme.com"
      domain only, "Tailscale push endpoint"). It is NEVER a full webhook
      URL, a bot token, a phone number, or an E.164 address: a webhook URL
      is a secret (anyone holding it can post as MOD), and a phone number
      is personal data. Store the recognizable-but-useless part.
    * `payload_summary` is the REDACTED payload — sizes and key names plus
      a truncated preview of at most 256 characters, never secrets: no
      tokens, no signed URLs, no user content that could contain PII
      beyond what the delivery itself already implies. Typical shape:
      {"bytes": 1840, "keys": ["text", "blocks"], "preview": "run 4217
      finished…", "files": 1}.

    No writer/reader code is wired here — the output nodes themselves run
    in containers (154/160); a later decomp row adds the API-side logger
    that writes these rows and the compliance view that reads them.

    FK style matches the other audit tables in this file: `tenant_id` has
    NO ondelete action (a tenant row cannot be hard-deleted out from under
    its own delivery trail), and `workflow_run_id` likewise carries no
    ON DELETE CASCADE — the delivery record outlives the run it describes.
    `api_task_id` is a plain Integer, NOT a FK: `api_task_queue.id` is an
    Integer PK and the queue is pruned/rotated, so a delivery row must not
    be tied to a queue row's lifetime.
    """
    __tablename__ = "output_delivery_log"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False)
    workflow_run_id = Column(ForeignKey("workflow_runs.id"), nullable=True)
    # Plain Integer, no FK — matches ApiTaskQueue.id (Integer PK).
    api_task_id = Column(Integer, nullable=True)
    node_id = Column(String(128), nullable=True)
    channel = Column(String(32), nullable=False)
    destination_hint = Column(String(255), nullable=True)
    status = Column(String(16), nullable=False)
    http_status = Column(Integer, nullable=True)
    attempt = Column(Integer, nullable=False, server_default=text("1"))
    payload_summary = Column(JSONB, nullable=False,
                             server_default=text("'{}'::jsonb"))
    error = Column(String(1024), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "channel IN ('slack', 'discord', 'email', 'sms', 'push', "
            "'webhook', 'file')",
            name="ck_output_delivery_log_channel"),
        CheckConstraint(
            "status IN ('delivered', 'failed', 'retrying', 'skipped')",
            name="ck_output_delivery_log_status"),
        Index("ix_output_delivery_log_tenant", "tenant_id"),
        Index("ix_output_delivery_log_run", "workflow_run_id"),
        # Compliance / retention view: newest deliveries for one tenant.
        Index("ix_output_delivery_log_tenant_created",
              "tenant_id", text("created_at DESC")),
    )


class OAuthConnection(Base):
    """sz-4.1 (sz-productivity-floor decomp §4.1, model + migration only) —
    `oauth_connections`: ONE row per external-provider account a tenant has
    connected (google / github / notion / linear / slack / airtable /
    microsoft365), the productivity-floor integrations read these to push
    MOD outputs into the customer's own account.

    No broker / no OAuth flows here — that is sz-4.2. This row is the
    credential VAULT: whatever the broker later mints, it lands here.

    WHAT IS STORED, AND WHY IT IS SAFE:
    * `encrypted_access_token` / `encrypted_refresh_token` are CIPHERTEXT
      only — the same convention as `http_triggers.encrypt_secret`: the
      value is `utils.encryption.encrypt_value(plain, <tenant key>)` with
      the key material from `services.key_provider` (per-TENANT key, so a
      key rotation invalidates the row and `key_version` is what tells the
      refresher it must re-connect). Plaintext tokens NEVER appear in this
      table, in a log line, or in `__repr__`.
    * `account_label` is the REDACTED display string — the masked email or
      account handle the UI shows ("a•••d@acme.com", "octocat"). It exists
      so the UI can name a connection without holding its identifier.
    * `external_account_id` is the provider's own opaque account id, used
      only for the partial-unique guard below; it is never rendered.
    * `last_error_code` is a CODE ONLY ("invalid_grant",
      "token_expired", "key_rotated") — never a provider response body,
      which is where providers leak account metadata.

    SOFT DELETE ONLY (ISO 27001 no-hard-delete): a disconnected
    connection gets `deleted_at` + `deleted_by` stamped and stays in the
    table; the partial UNIQUE index below is scoped to live rows, so
    re-connecting the same external account after a soft delete is legal
    while a second LIVE row for the same account is not.

    FK style matches the audit tables in this file: `tenant_id` and the
    optional `user_id` owner carry NO `ON DELETE` action at all — a tenant
    or user must not be dropped out from under the record of what it once
    connected. `user_id` NULL means a tenant-wide connection (no single
    owner).
    """
    __tablename__ = "oauth_connections"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False)
    # Owner who connected; NULL = tenant-wide connection.
    user_id = Column(ForeignKey("users.id"), nullable=True)
    provider = Column(String(32), nullable=False)
    account_label = Column(String(255), nullable=True)
    external_account_id = Column(String(255), nullable=True)
    scopes = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    encrypted_access_token = Column(Text, nullable=True)
    encrypted_refresh_token = Column(Text, nullable=True)
    key_version = Column(String(32), nullable=True)
    access_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_refreshed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(24), nullable=False,
                    server_default=text("'active'"))
    last_error_code = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "provider IN ('google', 'github', 'notion', 'linear', "
            "'slack', 'airtable', 'microsoft365')",
            name="ck_oauth_connections_provider"),
        CheckConstraint(
            "status IN ('active', 'refresh_failed', "
            "'reconnect_required', 'revoked')",
            name="ck_oauth_connections_status"),
        Index("ix_oauth_connections_tenant_provider", "tenant_id",
              "provider"),
        # At most ONE live connection per (tenant, provider, external
        # account): a soft-deleted tombstone never blocks a re-connect, and
        # rows with no external id (NULL) are outside the guard.
        Index("uq_oauth_connections_live_external", "tenant_id",
              "provider", "external_account_id", unique=True,
              postgresql_where=text("deleted_at IS NULL AND "
                                    "external_account_id IS NOT NULL")),
        # Pre-expiry refresh job: live-ish rows ordered by token expiry.
        Index("ix_oauth_connections_status_expiry", "status",
              "access_token_expires_at"),
    )

    def __repr__(self) -> str:
        return (f"<OAuthConnection id={self.id} tenant_id={self.tenant_id} "
                f"provider={self.provider} status={self.status}>")

    def __str__(self) -> str:
        return self.__repr__()


class WorkflowCrdtState(Base):
    """sz-3.1 (workflow-productivity-floor decomp §3.1, model + migration
    only) — `workflow_crdt_state`: the server-side persistence of the Yjs
    CRDT document behind the WorkflowBuilder canvas. ONE LIVE row per
    (tenant_id, workflow_id).

    The canvas itself (the WebSocket server, the y-postgres adapter, the
    awareness-update writer) is sz-3.2, a later decomp row. This class only
    establishes the storage shape so that layer has a table to target.

    * `ydoc_state` is the Yjs ENCODED state UPDATE (lib0 bytes), stored
      binary on purpose: it is not text and must never be re-encoded on the
      way in/out. `state_vector` is the encoded state vector (the set of
      update ids already folded in), so a reconnecting client can tell
      which updates it still needs.
    * SIZE IS THE SERVICE'S JOB, NOT THE TABLE'S (later item 3.2): sz-3.2
      caps `ydoc_state` and rotates into `workflow_crdt_snapshots` when the
      cap is hit. There is deliberately NO byte-length CHECK here — a CHECK
      would be re-evaluated on every awareness update and would make the
      table reject a legal write the moment the cap setting changed.
      `byte_size` + `update_count` exist so the service can make that
      decision without decoding the update.
    * The PARTIAL UNIQUE `uq_workflow_crdt_state_live` on
      (tenant_id, workflow_id) WHERE deleted_at IS NULL is what makes
      "one live row per canvas" a database fact rather than a service
      convention: a second live row for the same canvas is a hard
      IntegrityError, while a soft-deleted row never blocks a fresh live
      row.
    * SOFT DELETE ONLY (ISO 27001 no-hard-delete): tombstoning a canvas's
      state stamps `deleted_at` + `deleted_by` and keeps the row.
    * FK style matches the audit/vault tables in this file (CostRouting
      Decision, WorkflowOutputAuditLog, AuditEvent, PermissionAuditLog,
      PlatformSettingAudit, OAuthConnection): `tenant_id` and `workflow_id`
      carry NO `ON DELETE` action at all — neither a tenant nor a workflow
      may be dropped out from under the record of what its canvas once
      held.
    """
    __tablename__ = "workflow_crdt_state"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False)
    workflow_id = Column(ForeignKey("workflows.id"), nullable=False)
    ydoc_state = Column(LargeBinary, nullable=False)
    state_vector = Column(LargeBinary, nullable=True)
    update_count = Column(BigInteger, nullable=False,
                          server_default=text("0"))
    byte_size = Column(Integer, nullable=False,
                       server_default=text("0"))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("byte_size >= 0",
                        name="ck_workflow_crdt_state_byte_size"),
        # At most ONE live state row per canvas; a tombstone never blocks a
        # fresh live row.
        Index("uq_workflow_crdt_state_live", "tenant_id", "workflow_id",
              unique=True, postgresql_where=text("deleted_at IS NULL")),
    )

    def __repr__(self) -> str:
        return (f"<WorkflowCrdtState id={self.id} tenant_id={self.tenant_id} "
                f"workflow_id={self.workflow_id} "
                f"update_count={self.update_count} "
                f"byte_size={self.byte_size}>")

    def __str__(self) -> str:
        return self.__repr__()


class WorkflowCrdtSnapshots(Base):
    """sz-3.1 (workflow-productivity-floor decomp §3.1, model + migration
    only) — `workflow_crdt_snapshots`: the APPEND-ONLY snapshot history of
    a canvas's Yjs document, the disaster-recovery half of the CRDT
    persistence requirement (the spec's "recover from the latest snapshot
    with at most 60s of edits lost").

    The writer (the 60-second periodic snapshot job, the manual snapshot
    action, the pre-restore and shutdown writes) is sz-3.2, a later decomp
    row. This class only establishes the storage shape.

    * `ydoc_state` is the FULL Yjs state at this point — enough on its own
      to rebuild the document from scratch, which is what makes it the
      recovery path when the incremental `workflow_crdt_state` row is lost.
    * `graph_json` is the MATERIALIZED canvas, the same shape
      `workflows.graph_json` holds, so disaster recovery can restore a
      canvas even when no Yjs runtime is available to decode `ydoc_state`.
    * `sha256` is the hex sha256 of the canonical `ydoc_state` bytes: the
      dedup guard (an unchanged canvas does not get a new snapshot) and the
      integrity check on restore. The CHECK pins it to exactly 64
      characters — a truncated or padded digest is a corrupt row, not a
      cosmetic one.
    * `reason` says WHY the snapshot exists, so the restore picker can
      prefer a manual / pre_restore snapshot over a periodic one and so a
      shutdown snapshot is never mistaken for a user action. The CHECK makes
      the four reasons a database fact: 'periodic', 'manual', 'pre_restore',
      'shutdown'.
    * `created_by` NULL = written by the platform (the periodic / shutdown
      writer), not by a person.
    * `byte_size` is len(ydoc_state) as written, so the retention/rotation
      job can size history without decoding it.
    * SOFT DELETE ONLY (ISO 27001 no-hard-delete): pruning history stamps
      `deleted_at` + `deleted_by` and keeps the row; history is never
      hard-deleted, and recovery must still be possible after the owning
      workflow is itself soft-deleted — which is why neither FK carries an
      `ON DELETE` action (same style as the audit/vault tables in this
      file).
    * Indexed on (tenant_id, workflow_id, created_at DESC) because the
      restore read is "latest snapshot for this canvas"; the ordering is
      baked into the index, not left to the query.
    """
    __tablename__ = "workflow_crdt_snapshots"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False)
    workflow_id = Column(ForeignKey("workflows.id"), nullable=False)
    ydoc_state = Column(LargeBinary, nullable=False)
    graph_json = Column(JSONB, nullable=True)
    byte_size = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    reason = Column(String(32), nullable=False)
    created_by = Column(ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("LENGTH(sha256) = 64",
                        name="ck_workflow_crdt_snapshots_sha256_length"),
        CheckConstraint(
            "reason IN ('periodic', 'manual', 'pre_restore', "
            "'shutdown')",
            name="ck_workflow_crdt_snapshots_reason"),
        # "Latest snapshot for this canvas" — the restore read.
        Index("ix_workflow_crdt_snapshots_tenant_workflow_created",
              "tenant_id", "workflow_id", created_at.desc()),
    )

    def __repr__(self) -> str:
        return (f"<WorkflowCrdtSnapshots id={self.id} "
                f"tenant_id={self.tenant_id} workflow_id={self.workflow_id} "
                f"reason={self.reason} byte_size={self.byte_size}>")

    def __str__(self) -> str:
        return self.__repr__()


class WorkflowBreakpoint(Base):
    """sz-2.1 (workflow-productivity-floor decomp §2.1) — `workflow_breakpoints`:
    per-node breakpoints on the WorkflowBuilder canvas, the storage shape the
    live-debugging orchestrator reads before dispatching a node.

    Keyed by (workflow_version_id, node_id, user_id) per the spec: a breakpoint
    belongs to ONE user's debugging session on ONE canvas version, so two users
    can hold breakpoints on the same node of the same version at the same time
    and a new version of the workflow starts clean. The PARTIAL UNIQUE
    uq_workflow_breakpoints_live on that triple WHERE deleted_at IS NULL makes
    "at most one live breakpoint per (version, node, user)" a database fact: a
    duplicate live set is a hard IntegrityError, while a soft-deleted (cleared)
    breakpoint never blocks re-adding the same key.

    * `enabled` is the set/cleared toggle: clearing via right-click can either
      tombstone (ISO soft-delete) or flip enabled=false; the orchestrator's
      lookup index ix_workflow_breakpoints_tenant_version_enabled only ever
      indexes live + enabled rows, which is exactly the pause decision path.
    * `condition_json` is RESERVED for the later conditional-breakpoint row —
      NULL = unconditional pause. No CHECK on it: the shape of a future
      predicate language is not this table's contract.
    * `hit_count` / `last_hit_at` record how often the orchestrator actually
      paused at this breakpoint; the CHECK (hit_count >= 0) keeps a
      decrement-bug from silently corrupting the debug stats.
    * SOFT DELETE ONLY (ISO 27001 no-hard-delete): clearing a breakpoint
      stamps deleted_at + deleted_by and keeps the row — the record of what a
      user once paused on is never destroyed.
    * FK style matches the audit/vault tables in this file (CostRouting
      Decision, WorkflowOutputAuditLog, AuditEvent, PermissionAuditLog,
      PlatformSettingAudit, OAuthConnection, WorkflowCrdtState): NO `ON DELETE`
      action anywhere — not CASCADE, not SET NULL. A tenant, workflow,
      workflow version or user must never be dropped out from under the
      record of its breakpoints.
    """
    __tablename__ = "workflow_breakpoints"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id"), nullable=False)
    workflow_id = Column(ForeignKey("workflows.id"), nullable=False)
    workflow_version_id = Column(ForeignKey("workflow_versions.id"),
                                 nullable=False)
    node_id = Column(String(255), nullable=False)
    user_id = Column(ForeignKey("users.id"), nullable=False)
    enabled = Column(Boolean, nullable=False, server_default=text("true"))
    condition_json = Column(PostgresqlJSONB, nullable=True)
    hit_count = Column(Integer, nullable=False, server_default=text("0"))
    last_hit_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("LENGTH(node_id) > 0",
                        name="ck_workflow_breakpoints_node_id_nonempty"),
        CheckConstraint("hit_count >= 0",
                        name="ck_workflow_breakpoints_hit_count"),
        # At most ONE live breakpoint per (version, node, user); a tombstone
        # never blocks a fresh live row.
        Index("uq_workflow_breakpoints_live", "workflow_version_id",
              "node_id", "user_id", unique=True,
              postgresql_where=text("deleted_at IS NULL")),
        # The orchestrator's pause-decision lookup: live + enabled
        # breakpoints for a version, scoped by tenant.
        Index("ix_workflow_breakpoints_tenant_version_enabled",
              "tenant_id", "workflow_version_id",
              postgresql_where=text("deleted_at IS NULL AND enabled")),
    )

    def __repr__(self) -> str:
        return (f"<WorkflowBreakpoint id={self.id} tenant_id={self.tenant_id} "
                f"workflow_version_id={self.workflow_version_id} "
                f"node_id={self.node_id} user_id={self.user_id} "
                f"enabled={self.enabled} hit_count={self.hit_count}>")

    def __str__(self) -> str:
        return self.__repr__()


# wsip1.c+d -- the import block at the top of this file is frozen (other
# lanes own it); `Double` is imported here, next to the class that needs it,
# matching the mid-file `from sqlalchemy import ...` lines already in this
# module (2869, 7735). sqlalchemy.Double renders DOUBLE PRECISION in the
# Postgres dialect (sqlalchemy.Float would render FLOAT, not what the
# wsip1 decomp row asks for).
from sqlalchemy import Double


class ProductionDriftEvent(Base):
    """wsip1.c+d (workflow-self-improving-production decomp §1.c/§1.d) --
    `production_drift_events`: one row per DETECTED production-drift event,
    the append-only output of the drift sweep that reads
    `production_output_fingerprints` (sz-self-improving.1a, already landed).

    This table records detections, not outputs: `sample_examples` holds
    fingerprint ROW IDS only (references into production_output_fingerprints),
    never output content, so nothing here re-materializes a production
    artifact.

    `metric` is free-text (no CHECK/enum), same posture as
    `fingerprint_kind` on the sibling table -- the sweep owns valid values.
    Today it emits exactly two:
        'mmd'    -- moving-mean difference: |mean(candidate) - mean(reference)|
                    scaled against the reference spread.
        'zscore' -- how many reference-window standard deviations the
                    candidate sample sits out at.
    `value` is the computed metric score, `threshold` the cut the sweep
    applied when it decided this was drift; both DOUBLE PRECISION so a
    later sweep can re-rank / re-threshold without integer truncation.

    `reference_window_days` (default 30) / `candidate_window_days`
    (default 7) are the windows the detection compared, stored per row so
    a historical detection stays interpretable after the sweep's
    settings (platform-settings registry) change.

    Soft delete only (ISO 27001, no hard delete): `deleted_at` is a
    tombstone; there is no delete helper here or in the migration.
    `workflow_id` uses ON DELETE SET NULL (three words) so a drift event
    for a since-deleted workflow stays as history; `tenant_id` CASCADEs.
    """
    __tablename__ = "production_drift_events"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="SET NULL"),
                         nullable=True)
    # same free-text posture as fingerprint_kind on the sibling table:
    # which extractor kind this detection is about
    # (image_embedding | text_embedding | tensor_stats | schema_hash |
    # binary_hash | ...), owned by services/drift_fingerprint.py.
    fingerprint_kind = Column(String(40), nullable=False)
    # free text, owned by the drift sweep: 'mmd' | 'zscore' (see docstring)
    metric = Column(String(16), nullable=False)
    value = Column(Double, nullable=False)
    threshold = Column(Double, nullable=False)
    reference_window_days = Column(Integer, nullable=False,
                                   server_default=text("30"))
    candidate_window_days = Column(Integer, nullable=False,
                                   server_default=text("7"))
    # fingerprint row ids only (production_output_fingerprints.id), never
    # output content
    sample_examples = Column(JSONB, nullable=True)
    detail = Column(JSONB, nullable=True)
    detected_at = Column(DateTime(timezone=True), nullable=False,
                         server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_production_drift_events_workflow_detected",
            "workflow_id", "detected_at",
        ),
        Index(
            "ix_production_drift_events_tenant",
            "tenant_id",
        ),
    )


class CausalGraphEdge(Base):
    """wsip1.7 (workflow-self-improving-production decomp §1.7) --
    `causal_graph_edges`: one row per causal edge of a workflow's graph.

    The graph is a HYBRID of two layers, each owned by a pure service that
    writes this table later:
      * STRUCTURAL (``edge_kind='structural'``) -- edges derived from
        ``workflows.graph_json`` by services/causal_graph.py. Node ids only:
        ``source_feature``/``target_feature`` stay NULL, meaning "the node as
        a whole", and ``strength``/``sample_count`` are NULL (nothing was
        measured).
      * EMPIRICAL (``edge_kind='empirical'``) -- pairwise correlations between
        per-node output features and terminal-output features, computed by
        services/causal_correlations.py from sampled per-node run lineage.
        There ``strength`` is the correlation coefficient (-1..1) and
        ``sample_count`` how many sampled production runs it came from.

    ``generation`` is the staging-then-swap handle (tracker 5.3): a rebuild
    inserts a NEW generation rather than rewriting the live edge set, readers
    take MAX(generation) per workflow, and the swap soft-deletes the
    superseded generation. Default 0 keeps a plain writer valid.

    Uniqueness DEVIATES from the tracker on purpose: it asks for a unique
    constraint on (workflow_id, source_node_id, target_node_id, edge_kind),
    which cannot coexist with generations -- the same logical edge exists once
    per generation. So uniqueness is scoped to the generation AND to live
    rows (see the migration docstring), and the COALESCE legs collapse a NULL
    feature to '' so two feature-less structural edges really do collide.

    Soft delete only (ISO 27001, no hard delete): ``deleted_at`` is a tombstone
    and there is no delete helper here. Both FKs CASCADE -- the edge set is a
    workflow's graph, so it goes with its workflow and its tenant (unlike
    production_drift_events, whose detections outlive their workflow as
    history via ON DELETE SET NULL).
    """
    __tablename__ = "causal_graph_edges"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="CASCADE"),
                         nullable=False)
    # graph_json node ids (strings, not node_versions FKs): the causal graph
    # addresses graph positions, not pinned builds.
    source_node_id = Column(String(255), nullable=False)
    target_node_id = Column(String(255), nullable=False)
    # the tracker's edge_kind enum, enforced in DDL (see migration CHECK)
    edge_kind = Column(String(16), nullable=False)
    # NULL == "the node as a whole" (structural layer has no feature
    # granularity)
    source_feature = Column(String(64), nullable=True)
    target_feature = Column(String(64), nullable=True)
    # empirical layer only: the correlation coefficient. DOUBLE PRECISION
    # (`Double` already imported mid-file at models.py:16876), not FLOAT.
    strength = Column(Double, nullable=True)
    sample_count = Column(Integer, nullable=True)
    generation = Column(BigInteger, nullable=False, server_default=text("0"))
    computed_at = Column(DateTime(timezone=True), nullable=False,
                         server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "edge_kind IN ('structural', 'empirical')",
            name="ck_causal_graph_edges_kind",
        ),
        # Live-only uniqueness, generation-scoped (see docstring): the
        # COALESCE legs collapse a NULL feature to '' so feature-less edges
        # collide; the WHERE keeps tombstones out of the way of their
        # replacement generation.
        Index("uq_causal_graph_edges_live",
              "workflow_id", "generation", "source_node_id", "target_node_id",
              "edge_kind",
              func.coalesce(source_feature, text("''")),
              func.coalesce(target_feature, text("''")),
              unique=True,
              postgresql_where=text("deleted_at IS NULL"),
              sqlite_where=text("deleted_at IS NULL")),
        Index("ix_causal_graph_edges_tenant", "tenant_id"),
        # The reader is "this workflow's newest generation", so generation
        # sorts DESC.
        Index("ix_causal_graph_edges_workflow_generation",
              "workflow_id", generation.desc()),
    )


# wsip1.5 (workflow-self-improving-production tasks.md 1.5,
# sz-self-improving.1) -- APPEND AT models.py EOF. Uses names models.py
# already imports: Column, ForeignKey, BigInteger, Integer, String,
# DateTime, JSONB, Index, CheckConstraint, text, func.


class AdversaryWorkflow(Base):
    """wsip1.5 -- `adversary_workflows`: one row per production-workflow
    adversary PAIRING (Phase-3 adversarial co-evolution, design.md line
    147). For every production workflow a paired adversary runs on the
    per-tenant background-job pool: genetic-algorithm candidate inputs,
    sandbox dispatches (workflow_runs.purpose='adversary_dispatch'),
    recorded failures.

    `status` is the registrar's lifecycle machine (sz-self-improving.26
    create/teardown): 'active' | 'paused' | 'torn_down', CHECKed so the
    DB agrees with services/adversary_workflows.py. 'torn_down' is an
    explicit lifecycle state, distinct from the ISO 27001 `deleted_at`
    tombstone.

    `current_generation` is the GA generation this adversary has advanced
    to (services/adversary_ga.next_generation() bumps it); CHECKed >= 0
    so a decrement bug cannot silently rewind a resumed run.

    `config` records the knobs THIS pairing was created with (population
    size, GA seed, per-run budget, input_type) -- tunables live in the
    platform-settings registry, never env vars; this row keeps a
    historical pairing interpretable after those settings move.

    The PARTIAL UNIQUE uix_adversary_workflows_live_target on
    (target_workflow_id) WHERE deleted_at IS NULL AND status <> 'torn_down'
    makes "one LIVE adversary per production workflow" a database fact:
    a second registrar create for the same target is a hard IntegrityError,
    while a torn-down (or soft-deleted) pairing sits outside the index, so
    re-arming a workflow after teardown works.

    `target_workflow_id` CASCADEs (the pairing is meaningless without its
    target, so it is torn down with it); `tenant_id` CASCADEs. Soft delete
    only (ISO 27001): `deleted_at` is a tombstone, `torn_down_at` stamps
    the lifecycle teardown; there is no delete helper here.
    """
    __tablename__ = "adversary_workflows"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False)
    target_workflow_id = Column(ForeignKey("workflows.id",
                                           ondelete="CASCADE"),
                                nullable=False)
    status = Column(String(16), nullable=False,
                    server_default=text("'active'"))
    current_generation = Column(Integer, nullable=False,
                                server_default=text("0"))
    config = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())
    torn_down_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'torn_down')",
            name="ck_adversary_workflows_status",
        ),
        CheckConstraint(
            "current_generation >= 0",
            name="ck_adversary_workflows_generation_nonneg",
        ),
        # Partial unique index: exactly one LIVE adversary per production
        # workflow. Torn-down / soft-deleted pairings are outside the
        # index, so re-arming after teardown is allowed.
        Index(
            "uix_adversary_workflows_live_target",
            "target_workflow_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"
                                  " AND status <> 'torn_down'"),
            sqlite_where=text("deleted_at IS NULL"
                              " AND status <> 'torn_down'"),
        ),
        Index(
            "ix_adversary_workflows_tenant",
            "tenant_id",
        ),
    )


class AdversaryFinding(Base):
    """wsip1.5 -- `adversary_findings`: one row per PERSISTED adversary
    failure finding (sz-self-improving.30: the dispatch loop classifies
    each sandbox outcome via services/adversary_classification.classify_failure
    and persists the resulting Finding here, then fires
    adversary_found_failure_case / adversary_found_critical_failure on the
    event bus).

    `severity` is CHECKed over EXACTLY the SEVERITY_* constants of
    services/adversary_classification.py ('informational' | 'low' |
    'medium' | 'high' | 'critical', SEVERITY_RANK order) so the DB cannot
    hold a row the operator agent would rank differently than the
    classifier emitted it.

    FIELD MAPPING: the service names this field `failure_mode`
    (FAILURE_MODE_* = 'timeout' | 'error' | 'safety_scan_failure' |
    'critic_needs_fix'); the column is `failure_class` and the writer
    maps failure_mode -> failure_class 1:1. VARCHAR(64), NO CHECK: the
    service owns the mode vocabulary (same free-text posture as `metric`
    on production_drift_events) and a CHECK here would make every new
    failure mode a migration.

    `input_ref` holds candidate INPUT REFERENCES/IDS ONLY -- Candidate.id
    (e.g. 's13g0i4'), input_type, parent_ids, operators -- NEVER the
    candidate payload: a candidate may be image/tensor bytes and nothing
    here re-materializes one. `detail` carries the finding-side context:
    the already-truncated Finding.summary text (payload-free per
    adversary_classification._summary), the emit_critical_event flag, run
    ids. Finding.emit_critical_event is not a column -- it selects which
    event fires and is mirrored into `detail` for historical rows.

    `target_workflow_id` and `workflow_run_id` use ON DELETE SET NULL
    (three words, never ONDELETE) and are nullable: a finding for a
    since-deleted workflow/run is still the record of a real defect, so
    the row outlives its parent. `adversary_workflow_id` and `tenant_id`
    CASCADE: findings belong to the adversary pairing that produced them.

    Soft delete only (ISO 27001): `deleted_at` is a tombstone; no delete
    helper here or in the migration.
    """
    __tablename__ = "adversary_findings"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False)
    adversary_workflow_id = Column(ForeignKey("adversary_workflows.id",
                                              ondelete="CASCADE"),
                                   nullable=False)
    target_workflow_id = Column(ForeignKey("workflows.id",
                                           ondelete="SET NULL"),
                                nullable=True)
    workflow_run_id = Column(ForeignKey("workflow_runs.id",
                                        ondelete="SET NULL"),
                             nullable=True)
    generation = Column(Integer, nullable=False)
    severity = Column(String(16), nullable=False)
    # service field `failure_mode` -> column `failure_class` (see docstring)
    failure_class = Column(String(64), nullable=True)
    # candidate ids/refs only, never payload content
    input_ref = Column(JSONB, nullable=True)
    detail = Column(JSONB, nullable=True)
    found_at = Column(DateTime(timezone=True), nullable=False,
                      server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "severity IN ('informational', 'low', 'medium', 'high',"
            " 'critical')",
            name="ck_adversary_findings_severity",
        ),
        Index(
            "ix_adversary_findings_target_severity_found",
            "target_workflow_id", "severity", text("found_at DESC"),
        ),
        Index(
            "ix_adversary_findings_tenant",
            "tenant_id",
        ),
    )


# wsip1.6 (workflow-self-improving-production decomp, §1.6) -- append at
# models.py EOF (the lead appends this; the lane does not edit models.py).
class WizardDebugSession(Base):
    """wsip1.6 (workflow-self-improving-production decomp §1.6) --
    `wizard_debug_sessions`: one row per conversational-debug session
    opened from a completed run's "Why did this run produce this output?"
    button (design.md Decision 6; tasks.md 4.1 mode=debug + bound_run_id,
    4.3 debug SSE vocabulary, 4.4 wizard_quality_signal
    event_type=debug_fix_applied).

    This table records the SESSION, not the artifacts: `context_bundle_summary`
    holds sizes/ids only (bundle row ids, byte counts, node ids), never
    artifact content, so nothing here re-materializes a run artifact.
    `root_cause` / `proposed_fix` carry the design.md Decision 6 structured
    LLM shape ({root_cause_node_id, proposed_fix, expected_outcome,
    confidence}) split across the two columns, plus the causal-graph
    cross-check verdict.

    `status` mirrors the CLOSED session vocabulary, aligned with the EVT_*
    constants in services/wizard_debug.py (4.3):
        'debug_started'                <- EVT_DEBUG_STARTED
        'context_bundle_assembled'     <- EVT_CONTEXT_BUNDLE_ASSEMBLED
        'root_cause_identified'        <- EVT_ROOT_CAUSE_IDENTIFIED
        'fix_proposed'                 <- EVT_FIX_PROPOSED
        'fix_verification_started'     <- EVT_FIX_VERIFICATION_STARTED
        'fix_verification_succeeded'   <- EVT_FIX_VERIFICATION_SUCCEEDED
        'fix_verification_failed'      <- EVT_FIX_VERIFICATION_FAILED
    plus three session-terminal states with no SSE event of their own:
        'fix_applied'                  <- the 4.4 debug_fix_applied moment
                                          (Apply-card click; the SSE side
                                          already streamed apply_card_ready)
        'abandoned'                    <- user left before Apply
        'failed'                       <- pipeline error (verification or
                                          bundle assembly blew up)
    Deliberate renames from the brief's sketch: 'started' -> 'debug_started'
    and 'fix_verifying'/'fix_verified' -> 'fix_verification_started'/
    'fix_verification_succeeded' so every phase state is byte-identical to
    an EVT_* string; 'fix_verification_failed' is NOT a status (a failed
    verification leaves the session at 'fix_proposed' or moves it to
    'failed') -- see the migration for the same note.

    Soft delete only (ISO 27001, no hard delete): `deleted_at` is a
    tombstone; there is no delete helper here or in the migration.
    `user_id` / `workflow_id` / `bound_run_id` / `verification_run_id` use
    ON DELETE SET NULL (three words) so a session stays readable as history
    after its user / workflow / run are deleted; `tenant_id` CASCADEs.
    """
    __tablename__ = "wizard_debug_sessions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False)
    user_id = Column(ForeignKey("users.id", ondelete="SET NULL"),
                     nullable=True)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="SET NULL"),
                         nullable=True)
    bound_run_id = Column(ForeignKey("workflow_runs.id", ondelete="SET NULL"),
                          nullable=True)
    # closed vocabulary, aligned with services/wizard_debug.py EVT_* (see
    # docstring); CHECK enforced in the migration DDL.
    status = Column(String(32), nullable=False,
                    server_default=text("'debug_started'"))
    # sizes/ids only (bundle row ids, byte counts, node ids), never
    # artifact content
    context_bundle_summary = Column(JSONB, nullable=True)
    root_cause = Column(JSONB, nullable=True)
    proposed_fix = Column(JSONB, nullable=True)
    verification_run_id = Column(ForeignKey("workflow_runs.id",
                                            ondelete="SET NULL"),
                                 nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    applied_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'debug_started', 'context_bundle_assembled', "
            "'root_cause_identified', 'fix_proposed', "
            "'fix_verification_started', 'fix_verification_succeeded', "
            "'fix_verification_failed', 'fix_applied', "
            "'abandoned', 'failed')",
            name="wizard_debug_sessions_status",
        ),
        Index(
            "ix_wizard_debug_sessions_tenant_created",
            "tenant_id", "created_at",
        ),
        Index(
            "ix_wizard_debug_sessions_bound_run",
            "bound_run_id",
        ),
    )


# wsip 1.8 (workflow-self-improving-production decomp, §1.8) -- ORM view of
# the two autonomous-operator tables created by
# migrations/20261012g_autonomous_operator_tables.py. APPEND AT models.py EOF.
# `Double` is already imported at models.py line 16876 (next to
# ProductionDriftEvent); JSONB / func / text / Index / CheckConstraint are
# module-level imports. Style mirrors class ProductionDriftEvent.
#
# NOTE: the CHECK constraints below are named (ck_*) so the migration's
# re-run guard and any later pg_constraint introspection can find them; the
# migration's inline CHECKs are unnamed, so if the lead wants the ORM names
# to appear in the live DB, the migration statements need the same
# `CHECK (...)` with `name=` -- the DDL text here matches the migration
# exactly, SQLAlchemy just adds the generated/auto name on create_all.


class AutonomousOperatorProposal(Base):
    """wsip 1.8 -- `autonomous_operator_proposals`: one row per fix the
    autonomous operator files (design.md Phase 4 propose-only -> Phase 5
    full-auto). The pure services already landed --
    services/autonomous_degradation.py (DegradationScore.score +
    DegradationEvidence), services/autonomous_propose_fix.py (fix prompt +
    wizard-event outcomes) -- compute everything; this table is where the
    Operator Proposals dashboard (7.6) and the per-(workflow, day) fix cap
    (7.7, max 3) read it.

    `status` is a CLOSED set (CHECK, unlike the free-text metric/fingerprint
    posture on production_drift_events): the dashboard filters on it and
    7.5's writer transitions it, so a typo'd state must fail at insert:
        proposed | blocked_on_user | approved | rejected | deployed |
        superseded | failed
    'blocked_on_user' is the propose-only phase-4 hold; 'deployed' means a
    row exists in autonomous_operator_deployments; 'superseded' is the soft
    replacement state (no hard delete, ISO 27001).

    `evidence` / `wizard_lineage` / `proposed_graph_json` are JSONB holding
    ids + metrics only (drift_status, error rates, predictor Brier scores,
    adversary severities; test-run ids + wizard session/proposal ids):
    nothing here re-materializes a production artifact.
    `fix_prompt` is TEXT (the synthesized prompt, capped by the
    autonomous_operator.fix_prompt_max_chars platform setting, not an env
    var). `degradation_score` is DOUBLE PRECISION, the float composite.

    `workflow_id` CASCADEs (a proposal is a fix FOR this workflow: 7.7
    counts per (workflow, day), 7.6 lists per workflow -- the row is
    meaningless without its parent); `base_version_id` / `reviewed_by` use
    ON DELETE SET NULL (three words) so a since-deleted base version or a
    deleted reviewer still resolves as history.

    Soft delete only (ISO 27001): `deleted_at` is a tombstone; no delete
    helper here.
    """
    __tablename__ = "autonomous_operator_proposals"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="CASCADE"),
                         nullable=False)
    status = Column(String(24), nullable=False,
                    server_default=text("'proposed'"))
    degradation_score = Column(Double, nullable=True)
    evidence = Column(JSONB, nullable=True)
    fix_prompt = Column(Text, nullable=True)
    proposed_graph_json = Column(JSONB, nullable=True)
    wizard_lineage = Column(JSONB, nullable=True)
    base_version_id = Column(ForeignKey("workflow_versions.id",
                                        ondelete="SET NULL"),
                             nullable=True)
    reviewed_by = Column(ForeignKey("users.id", ondelete="SET NULL"),
                         nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'blocked_on_user', 'approved', "
            "'rejected', 'deployed', 'superseded', 'failed')",
            name="ck_autonomous_operator_proposals_status",
        ),
        Index(
            "ix_autonomous_operator_proposals_workflow_created",
            "workflow_id", "created_at",
        ),
        Index(
            "ix_autonomous_operator_proposals_tenant_status",
            "tenant_id", "status",
        ),
    )


class AutonomousOperatorDeployment(Base):
    """wsip 1.8 -- `autonomous_operator_deployments`: one row per canary
    deployment carrying a proposal into production (8.x canary mechanics).
    services/autonomous_canary.py is pure: route_variant() splits traffic
    deterministically and evaluate_canary() returns a verdict; this table
    records the window so a rollback can name what was tried.

    `stage` is the CLOSED canary vocabulary actually emitted by
    services/autonomous_canary.py (grep of its literals):
        route_variant()        -> Literal["canary", "production"]
        CanaryDecision.action  -> Literal["rollback", "promote", "continue"]
    Mapping:
        'canary'     -- window live: candidate receives its canary share.
        'continue'   -- evaluate_canary() said continue (also the
                        'insufficient_runs' reason): keep the window open.
        'promote'    -- verdict promote: widen toward 100%.
        'rollback'   -- verdict rollback: kill the candidate.
        'production' -- terminal: route_variant() production arm, the
                        promoted candidate owns 100% of traffic.
    No other stage name is invented here.

    `traffic_pct` is DOUBLE PRECISION (the wsip.autonomous_operator_canary_pct
    split in force for this window, kept per row so a historical window stays
    interpretable after the setting moves); `metrics` is JSONB CanaryStats-
    shaped window evidence + the CanaryThresholds snapshot the verdict used.

    `proposal_id` FKs the proposal this deployment executed (CASCADE: the
    deployment is meaningless without what it deployed); `new_version_id`
    uses ON DELETE SET NULL (three words) so a rolled-back/deleted version
    still names what was tried. Soft delete only (ISO 27001).
    """
    __tablename__ = "autonomous_operator_deployments"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False)
    proposal_id = Column(ForeignKey("autonomous_operator_proposals.id",
                                    ondelete="CASCADE"),
                         nullable=False)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="CASCADE"),
                         nullable=False)
    new_version_id = Column(ForeignKey("workflow_versions.id",
                                       ondelete="SET NULL"),
                            nullable=True)
    stage = Column(String(24), nullable=False)
    traffic_pct = Column(Double, nullable=True)
    metrics = Column(JSONB, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "stage IN ('canary', 'continue', 'promote', 'rollback', "
            "'production')",
            name="ck_autonomous_operator_deployments_stage",
        ),
        Index(
            "ix_autonomous_operator_deployments_proposal",
            "proposal_id",
        ),
        Index(
            "ix_autonomous_operator_deployments_workflow_started",
            "workflow_id", "started_at",
        ),
    )


# --- 451 / expr-1.8: persistent workflow memory (lead: append at models.py EOF)
# Names used here are already in models.py's namespace at EOF:
#   Column, ForeignKey, BigInteger, Integer, String, Double, JSONB (the
#   TypeDecorator at models.py:40), DateTime, func, text, Index.
# `Double` is imported mid-file (models.py:16876) -- appending at EOF is fine.
#
# SLUG, NOT FK: memory is keyed (tenant_id, workflow_slug) per design Decision
# 11 / risk line ~155 so renames preserve memory. The workflows table has no
# column literally named `slug`; the slug-like column is `workflows.name`
# (models.py:2127, String(255)) -- services/sub_workflow_validation.py:110
# already resolves a sub-workflow ref's `workflow_slug` via
# `models.Workflow.name == workflow_slug`. No FK here on that: a FK would
# re-point or drop memory on rename/parent-delete, the opposite of the design.
#
# QUOTA: no column added to the existing `tenants` table (bind-mounted live,
# additive-only). lead: quota via aggregate SUM(size_bytes) or a platform
# settings tunable.
#
# EMBEDDING: migrations/20261012h_workflow_memory_tables.py lands
# `embedding VECTOR(768) NOT NULL` when pgvector is present and
# `embedding JSONB NOT NULL` when it is not (the app DB is postgres:13, no
# contrib -> JSONB today). models.py has NO ORM class for
# `vector_search_embeddings` (20261011a's table) to copy from -- the only
# embedding column in models.py is wizard_learned_crossovers'
# `embedding = Column(JSONB, ...)` (models.py:10706, cosine-in-Python,
# explicitly "JSONB, not pgvector"). This snippet mirrors that exact posture:
# the ORM view is JSONB, so it matches the JSONB branch the test DB provisions
# and keeps the pgvector branch a DDL-only ANN index. If an ORM pgvector view
# is ever wanted it lands as a `Vector(768)` TypeDecorator in its own change.


class WorkflowMemoryKV(SoftDeleteMixin, Base):
    """expr-1.8 (workflow-expressiveness-primitives 1.8 / 9.7-9.9) -- the
    named-state scratchpad: one live row per (tenant, workflow_slug, key).

    `memory_write_kv` / `memory_read_kv` (9.7/9.8) own the read/write path;
    this class is storage only, no helper is wired here. `expires_at` is the
    per-row expiry the retention sweep (9.11) reads; `size_bytes` feeds the
    per-tenant quota gate (9.9). Soft delete only (ISO 27001): the partial
    UNIQUE is live-only, so a tombstone never blocks a later re-write of the
    same key and a re-write never collides with its own tombstone.
    """
    __tablename__ = "workflow_memory_kv"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    workflow_slug = Column(String(255), nullable=False, index=True)
    key = Column(String(512), nullable=False)
    value = Column(JSONB, nullable=False)
    size_bytes = Column(BigInteger, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        Index("uq_workflow_memory_kv_live", "tenant_id", "workflow_slug",
              "key", unique=True, postgresql_where=text("deleted_at IS NULL")),
    )


class WorkflowMemoryVector(SoftDeleteMixin, Base):
    """expr-1.8 -- the embedding-indexed RAG store: one live row per
    (tenant, workflow_slug, collection, doc_id).

    `memory_embed_and_index` / `memory_vector_search` (9.8/9.9 side) own the
    embed + search path. `content_ref` is JSONB refs/ids ONLY -- never a raw
    blob -- so a memory row never re-materializes a production artifact
    (same posture as production_drift_events' `sample_examples`).
    `embedding` is JSONB (list[float]) in the ORM view: the JSONB branch is
    what the app DB provisions today and what the Python scorer reads back;
    the pgvector branch adds only the ivfflat ANN index in DDL.
    """
    __tablename__ = "workflow_memory_vector"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    workflow_slug = Column(String(255), nullable=False, index=True)
    collection = Column(String(128), nullable=False,
                        server_default=text("'default'"), index=True)
    doc_id = Column(String(512), nullable=False)
    embedding = Column(JSONB, nullable=False)
    content_ref = Column(JSONB, nullable=True)
    # NOTE (reviewer fix): `metadata` is reserved on a declarative Base (it
    # collides with Base.metadata) -- same fix already used at
    # models.py:4713 (`metadata_ = Column("metadata", JSONB, ...)`).
    metadata_ = Column("metadata", JSONB, nullable=True)
    size_bytes = Column(BigInteger, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        Index("uq_workflow_memory_vector_live", "tenant_id", "workflow_slug",
              "collection", "doc_id", unique=True,
              postgresql_where=text("deleted_at IS NULL")),
    )


class WorkflowMemoryTimeseries(SoftDeleteMixin, Base):
    """expr-1.8 -- the per-timestamp metric store: one row per
    (tenant, workflow_slug, series) observation at `ts`.

    `memory_timeseries_append` / `memory_timeseries_query` (9.10/9.11 side)
    own append + aggregate. `value` is DOUBLE PRECISION (nullable: a
    non-numeric observation is a tag-only row). The retention sweep (9.11)
    soft-deletes past the per-workflow `memory_retention` window (default
    90d for timeseries, keep_forever for kv/vector) -- never a DELETE.
    The migration's index orders `ts DESC` for the window query; the ORM
    Index() carries no sort order, so it is the same key set, plain order.
    """
    __tablename__ = "workflow_memory_timeseries"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    workflow_slug = Column(String(255), nullable=False, index=True)
    series = Column(String(255), nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    value = Column(Double, nullable=True)
    tags = Column(JSONB, nullable=True)
    size_bytes = Column(BigInteger, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        Index("ix_workflow_memory_timeseries_series_ts", "tenant_id",
              "workflow_slug", "series", "ts"),
    )


# --- APPEND AT models.py EOF (task 454, wsip1.3+1.4) --------------------------
# No new imports needed: BigInteger, Boolean, CheckConstraint, Column, ForeignKey,
# Index, Integer, SmallInteger, String, Double (line 16876), func, text and the
# custom JSONB TypeDecorator are all already in models.py.


class FailurePredictionModel(Base):
    """wsip1.3+1.4 (workflow-self-improving-production decomp §1.3/§1.4) --
    `failure_prediction_models`: the model store services/failure_predictor.py
    was written against. The service is PURE -- dump_model() hands the caller an
    HMAC-SHA256-signed BYTES blob (MAGIC || joblib payload || digest) and load_model
    verifies that signature before unpickling, so a tampered `model_blob` row can
    NEVER reach the unpickler. `blob_ref` is the JSONB S3-pointer alternative for
    blobs too big to keep inline; the table CHECKs at least one of the two is
    present.

    `scope` ('workflow' | 'global') + nullable tenant_id/workflow_id encode the
    WSIP 3.3 lookup: a workflow-specific model, else the GLOBAL fallback model
    (tenant_id NULL, workflow_id NULL, scope 'global'). `training_rows` makes the
    spec's >=100-runs gate visible per model; `metrics` carries the CalibrationTracker
    output (count, brier, buckets) plus AUC.

    Exactly one live active model per workflow (uq_..._active_workflow) and one
    active global fallback (uq_..._active_global): the nightly trainer stages a
    new model, then swaps `is_active` -- the superseded row stays as history.
    Soft delete only (ISO 27001): `deleted_at` is a tombstone; there is no delete
    helper here or in the migration. Both FKs CASCADE: a model belongs to its
    workflow/tenant outright (the FEATURES table keeps pointing at models via its
    own ON DELETE SET NULL FK, so training history survives).
    """
    __tablename__ = "failure_prediction_models"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    # NULL = the global fallback model (WSIP 3.3) -- not owned by any tenant
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=True)
    # NULL = global-scope model
    workflow_id = Column(ForeignKey("workflows.id", ondelete="CASCADE"),
                         nullable=True)
    scope = Column(String(16), nullable=False)
    # exactly dump_model() output; load_model refuses anything whose HMAC
    # signature does not verify
    model_blob = Column(LargeBinary, nullable=True)
    # S3 pointer (bucket/key JSON) for oversized blobs
    blob_ref = Column(JSONB, nullable=True)
    training_rows = Column(Integer, nullable=False,
                           server_default=text("0"))
    # CalibrationTracker output: count / brier / buckets (+ auc)
    metrics = Column(JSONB, nullable=True)
    trained_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    is_active = Column(Boolean, nullable=False, server_default=text("FALSE"))
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("scope IN ('workflow', 'global')",
                        name="ck_failure_prediction_models_scope"),
        CheckConstraint("(model_blob IS NOT NULL) OR (blob_ref IS NOT NULL)",
                        name="ck_failure_prediction_models_blob_or_ref"),
        Index(
            "uq_failure_prediction_models_active_workflow",
            "workflow_id",
            unique=True,
            postgresql_where=text(
                "is_active AND deleted_at IS NULL AND workflow_id IS NOT NULL"
            ),
            sqlite_where=text(
                "is_active AND deleted_at IS NULL AND workflow_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_failure_prediction_models_active_global",
            text("(1)"),
            unique=True,
            postgresql_where=text(
                "is_active AND scope = 'global' AND deleted_at IS NULL"
            ),
            sqlite_where=text(
                "is_active AND scope = 'global' AND deleted_at IS NULL"
            ),
        ),
    )


class FailurePredictionFeature(Base):
    """wsip1.4 (workflow-self-improving-production decomp §1.4) --
    `failure_prediction_features`: one row per DISPATCH-TIME prediction, the
    append-only training + calibration log services/failure_predictor.py feeds.

    `features` is the extract_features() dict: exactly FEATURE_ORDER keys, all
    floats (node/edge/input counts, byte sizes, hour/day, recent failure rates,
    accel-kind one-hot). NUMERIC SIGNALS ONLY -- no payload content, no file
    names, no user data; nothing here re-materializes a run's inputs.
    `predicted_failure_probability` is predict() output under the module-wide
    label convention: **1 = FAILURE**, 0 = success -- so `actual_outcome` uses
    the same coding, filled POST-RUN (NULL until the backfill sweep records it;
    the partial ix_..._unrecorded index exists exactly for that sweep).
    `mode` is the WSIP mode this prediction ran under; only 'shadow' produces a
    record (shadow_record() returns None for 'off'/'enforce').
    `model_id` says which model scored the row (NULL = scored before any model
    existed); FK SET NULL so deleting a model never erases its training rows.

    Soft delete only (ISO 27001): `deleted_at` is a tombstone; no delete helper
    here or in the migration. `tenant_id` CASCADEs (real teardown);
    workflow/workflow_run FKs SET NULL so predictions outlive their parents as
    training history.
    """
    __tablename__ = "failure_prediction_features"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="SET NULL"),
                         nullable=True)
    workflow_run_id = Column(ForeignKey("workflow_runs.id", ondelete="SET NULL"),
                             nullable=True)
    # extract_features() dict: FEATURE_ORDER float keys only
    features = Column(JSONB, nullable=False)
    # P(FAILURE) -- module-wide convention, see docstring
    predicted_failure_probability = Column(Double, nullable=True)
    mode = Column(String(16), nullable=False)
    model_id = Column(ForeignKey("failure_prediction_models.id",
                                 ondelete="SET NULL"),
                      nullable=True)
    # 1 = FAILURE (module-wide), filled post-run
    actual_outcome = Column(SmallInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    outcome_recorded_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("mode IN ('off', 'shadow', 'enforce')",
                        name="ck_failure_prediction_features_mode"),
        CheckConstraint("actual_outcome IN (0, 1)",
                        name="ck_failure_prediction_features_outcome"),
        Index(
            "ix_failure_prediction_features_workflow_created",
            "workflow_id", "created_at",  # DESC in the migration (source of truth)
        ),
        Index(
            "ix_failure_prediction_features_tenant",
            "tenant_id",
        ),
        Index(
            "ix_failure_prediction_features_unrecorded",
            "created_at",
            postgresql_where=text("actual_outcome IS NULL"),
            sqlite_where=text("actual_outcome IS NULL"),
        ),
    )


class SubgraphEquivalenceIndex(Base):
    """expr-1.4 (workflow-expressiveness-primitives) --
    `subgraph_equivalence_index`: one row per (workflow, subgraph) with its
    canonical hash, for the daily analyzer computing canonical hashes and
    equivalence-class membership.

    `canonical_hash` is a SHA-256 hexdigest (64 chars) from the
    subgraph_canonical service; VARCHAR(128) provides headroom.

    `hash_kind` = 'byte' (canonical AST normalisation, default) or
    'semantic' (future ML-based equivalence signal). CHECK enforced.

    `node_ids` is a JSONB array of node ID strings — the induced subgraph
    membership. A md5-based unique index on the text representation avoids
    the Postgres JSONB-unique-index limitation.

    Soft delete only (ISO 27001, no hard delete) applies to analyzer-driven
    tombstoning via `deleted_at`. `tenant_id` and `workflow_id` CASCADE on
    delete (real teardown -- orphaned rows from a deleted workflow would
    poison equivalence-class counts). `workflow_version_id` uses ON DELETE
    SET NULL so the row survives a version delete.
    """
    __tablename__ = "subgraph_equivalence_index"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False)
    workflow_id = Column(ForeignKey("workflows.id", ondelete="CASCADE"),
                         nullable=False)
    workflow_version_id = Column(ForeignKey("workflow_versions.id",
                                            ondelete="SET NULL"),
                                 nullable=True)
    # SHA-256 hexdigest from subgraph_canonical.canonical_hash(): 64 chars.
    canonical_hash = Column(String(128), nullable=False)
    # 'byte' = canonical AST normalisation; 'semantic' = ML-based future.
    hash_kind = Column(String(16), nullable=False,
                       server_default=text("'byte'"))
    # JSONB array of node IDs, e.g. '["n1","n2","n3"]'
    node_ids = Column(JSONB, nullable=False)
    node_count = Column(Integer, nullable=False)
    computed_at = Column(DateTime(timezone=True), nullable=False,
                         server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "hash_kind IN ('byte', 'semantic')",
            name="ck_subgraph_equivalence_index_hash_kind",
        ),
        Index(
            "ix_subgraph_equivalence_index_tenant_hash",
            "tenant_id", "canonical_hash",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_subgraph_equivalence_index_cross_tenant",
            "canonical_hash",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_subgraph_equivalence_index_workflow",
            "workflow_id",
        ),
        Index(
            "uq_subgraph_equivalence_index_live",
            "workflow_id", "hash_kind", "canonical_hash",
            text("md5(node_ids::text)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class SecretAccessLog(Base):
    """FORTRESS 3.2 — append-only secret access log (table from 20261012q; trigger blocks UPDATE/DELETE)."""
    __tablename__ = "secret_access_log"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    workflow_run_id = Column(Integer, ForeignKey("workflow_runs.id"), nullable=True)
    node_run_id = Column(Integer, ForeignKey("node_runs.id"), nullable=True)
    node_id = Column(Text, nullable=True)
    secret_name = Column(Text, nullable=False)
    accessor = Column(Text, nullable=False)
    outcome = Column(Text, nullable=False)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())


class EgressDenialLog(Base):
    """FORTRESS 3.3 — append-only egress denial log (table from 20261012q; trigger blocks UPDATE/DELETE)."""
    __tablename__ = "egress_denial_log"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    workflow_run_id = Column(Integer, ForeignKey("workflow_runs.id"), nullable=True)
    node_run_id = Column(Integer, ForeignKey("node_runs.id"), nullable=True)
    node_id = Column(Text, nullable=True)
    destination_host = Column(Text, nullable=False)
    destination_port = Column(Integer, nullable=True)
    rule = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())


class Certifier(Base):
    """FORTRESS 5.9 — trusted third-party certifier (migration 20261012v). Soft delete only."""
    __tablename__ = "certifiers"

    id = Column(BigInteger, primary_key=True)
    name = Column(String(255), nullable=False)
    public_key_b64 = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, server_default=text("'active'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="ck_certifiers_status"),
    )


class WorkflowCertification(Base):
    """FORTRESS 5.9 — certification of a workflow by a certifier (migration 20261012v). Soft delete only."""
    __tablename__ = "workflow_certifications"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
    workflow_id = Column(Integer, ForeignKey("workflows.id", ondelete="RESTRICT"), nullable=False)
    certifier_id = Column(BigInteger, ForeignKey("certifiers.id", ondelete="SET NULL"), nullable=True, index=True)
    status = Column(String(16), nullable=False, server_default=text("'active'"))
    issued_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    revoke_reason = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    signature_b64 = Column(Text, nullable=True)
    claim_sha256 = Column(Text, nullable=True)
    certification_kind = Column(Text, nullable=False, server_default=text("'platform'"))

    __table_args__ = (
        CheckConstraint("status IN ('active', 'revoked', 'expired')",
                        name="ck_workflow_certifications_status"),
        CheckConstraint("expires_at > issued_at",
                        name="ck_workflow_certifications_expires_after_issued"),
        CheckConstraint("certification_kind IN ('platform', 'third_party')",
                        name="ck_workflow_certifications_kind"),
    )


class NodeRunAttestation(Base):
    """FORTRESS.2.3c — worker per-task Ed25519 signature over its contribution (20261012x).

    Append-only: UPDATE/DELETE/TRUNCATE raise via fortress_13_append_only_guard.
    One attestation per (node_run_id, task_id) attempt.
    """
    __tablename__ = "node_run_attestations"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
    workflow_run_id = Column(Integer, ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=False)
    node_run_id = Column(Integer, ForeignKey("node_runs.id", ondelete="RESTRICT"), nullable=False)
    task_id = Column(Integer, ForeignKey("api_task_queue.id", ondelete="RESTRICT"), nullable=False)
    worker_name = Column(Text, nullable=False)
    alg = Column(Text, nullable=False)
    key_id = Column(Text, nullable=False)
    public_key_b64 = Column(Text, nullable=False)
    payload_sha256 = Column(Text, nullable=False)
    signature_b64 = Column(Text, nullable=False)
    contribution = Column(PostgresqlJSONB, nullable=False)
    signed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    attested_region = Column(Text, nullable=True)
    attested_storage = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("node_run_id", "task_id", name="uq_node_run_attestations_run_task"),
        CheckConstraint("alg = 'ed25519'", name="ck_node_run_attestations_alg"),
        CheckConstraint("LENGTH (payload_sha256) = 64", name="ck_node_run_attestations_sha256_length"),
    )


class WorkflowRunAttestation(Base):
    """FORTRESS.4.5 S1a — per-run compliance evidence (20261013a), append-only.

    One row per runtime EVENT, not per run: a claim inserts
    status='dispatched'; the completion inserts a SECOND row
    status='completed'/'failed' whose referenced_dispatch_id points at the
    dispatch row. Matches migrations/20261013a_fortress_45_workflow_run_attestations.py
    exactly — where the design doc and the migration differ, the migration
    wins (its UNIQUE (workflow_run_id, node_run_id) correction: a plain INDEX
    ix_wra_run_node, not unique — the dispatch row and its completion row for
    the SAME node coexist).

    Append-only: UPDATE/DELETE/TRUNCATE raise via fortress_13_append_only_guard
    (the shared trigger from fortress 1.3, reused — not declared here, DB
    trigger only). No relationship() — read via explicit queries.
    """
    __tablename__ = "workflow_run_attestations"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
    workflow_run_id = Column(Integer, ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=False)
    node_run_id = Column(Integer, ForeignKey("node_runs.id", ondelete="RESTRICT"), nullable=True)
    compliance_tag = Column(String(64), nullable=False)
    dispatch_mode = Column(String(16), nullable=False, server_default="enforce")
    claimed_worker_name = Column(Text, nullable=True)
    claimed_worker_region = Column(Text, nullable=True)
    claimed_worker_isolation = Column(String(32), nullable=True)
    claimed_worker_encrypted = Column(Boolean, nullable=True)
    storage_connection_id = Column(String(128), nullable=True)
    storage_region = Column(String(64), nullable=True)
    storage_encrypted = Column(Boolean, nullable=True)
    tag_satisfied = Column(Boolean, nullable=False, server_default=text("true"))
    tag_violation_detail = Column(Text, nullable=True)
    completed_worker_name = Column(Text, nullable=True)
    completed_region = Column(String(64), nullable=True)
    completed_storage = Column(String(128), nullable=True)
    status = Column(String(16), nullable=False, server_default="dispatched")
    referenced_dispatch_id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("workflow_run_attestations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('dispatched', 'completed', 'failed')",
            name="ck_wra_status_values",
        ),
        CheckConstraint(
            "dispatch_mode IN ('enforce', 'shadow', 'off')",
            name="ck_wra_dispatch_mode",
        ),
        CheckConstraint(
            "(status = 'dispatched') = (referenced_dispatch_id IS NULL)",
            name="ck_wra_dispatch_rows_have_no_reference",
        ),
        Index(
            "uq_wra_one_completion_per_dispatch",
            "referenced_dispatch_id",
            unique=True,
            postgresql_where=text("referenced_dispatch_id IS NOT NULL"),
            sqlite_where=text("referenced_dispatch_id IS NOT NULL"),
        ),
        Index("ix_wra_run_node", "workflow_run_id", "node_run_id"),
        Index("ix_wra_tenant_tag_created", "tenant_id", "compliance_tag", "created_at"),
        Index("ix_wra_run", "workflow_run_id"),
        Index("ix_wra_tag_satisfied", "tag_satisfied"),
    )


class MpcSessionState(Base):
    """FORTRESS.6.7 S1a — CURRENT-state snapshot of one MPC session (20261012z).

    One row per session, never per transition: proposed -> ready -> running ->
    completed|failed, with rejected|cancelled as the reject/cancel terminals.
    Transition history lives in the existing append-only audit trail; this
    table only ever holds "now", so UPDATE is ALLOWED here (unlike the
    append-only attestation/ledger tables).

    Matches migrations/20261012z_fortress_67_mpc_session_state.py exactly:
    workflow_id + initiator_tenant_id FKs are ON DELETE RESTRICT (the row IS
    the compliance record of who jointly computed what); the nullable user FKs
    are ON DELETE SET NULL. ``id`` is BIGSERIAL PRIMARY KEY.

    No SoftDeleteMixin: deleted_at/deleted_by_user_id are declared inline so
    the column set mirrors the migration one-for-one (same idiom as
    OutputScanExemption). The migration's no-hard-delete guard
    (trg_no_delete_/trg_no_truncate_ mpc_session_state RAISE feature_not_supported
    via mpc_session_state_no_hard_delete) is a DB trigger only — SQLAlchemy
    does not reproduce it; ORM .delete() fails at flush against the live DB.
    No relationship() — read via explicit queries.
    """
    __tablename__ = "mpc_session_state"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    workflow_id = Column(Integer, ForeignKey("workflows.id", ondelete="RESTRICT"), nullable=False)
    initiator_tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
    status = Column(String(16), nullable=False, server_default="proposed")
    participants_json = Column(PostgresqlJSONB, nullable=False, server_default=text("'[]'::jsonb"))
    security_props_json = Column(PostgresqlJSONB, nullable=False)
    aggregate_result_ref = Column(Text, nullable=True)
    error_code = Column(String(64), nullable=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'ready', 'running', 'completed', "
            "'failed', 'rejected', 'cancelled')",
            name="ck_mpc_session_state_status",
        ),
        Index(
            "ix_mpc_session_state_initiator_status",
            "initiator_tenant_id", "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_mpc_session_state_participants_gin",
            "participants_json",
            postgresql_using="gin",
            postgresql_ops={"participants_json": "jsonb_path_ops"},
        ),
    )


class DpBudgetLedger(Base):
    """FORTRESS.6.3 S1/S3 — one row per dispatched DP node: how much (epsilon,
    delta) this run's use of that node consumed in a YYYY-MM period.

    APPEND-ONLY (DB trigger fortress_63_append_only_guard, ENABLE ALWAYS):
    UPDATE/DELETE/TRUNCATE raise feature_not_supported. Never call
    db.query(DpBudgetLedger)... .update()/.delete() — always INSERT.
    Matches migrations/20261012y_fortress_63_dp_budget_ledger.py exactly
    (ledger FKs are all ON DELETE RESTRICT, workflow_id NOT NULL — only the
    CAPS table's workflow_id is nullable).
    """
    __tablename__ = "dp_budget_ledger"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
    workflow_id = Column(Integer, ForeignKey("workflows.id", ondelete="RESTRICT"), nullable=False)
    node_id = Column(String(128), nullable=False)
    run_id = Column(Integer, ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=False)
    epsilon_spent = Column(Numeric(12, 6), nullable=False)
    delta_spent = Column(Numeric(20, 18), nullable=False)
    period = Column(String(10), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "epsilon_spent >= 0 AND delta_spent >= 0 AND delta_spent < 1",
            name="ck_dp_budget_ledger_spend_nonneg",
        ),
        # Postgres-only regex CHECK (the migration guards it with no dialect
        # branch because it only ever runs against Postgres); SQLite's `~`
        # operator does not exist, so declaring this CheckConstraint here
        # unconditionally would break `Base.metadata.create_all()` against
        # the sqlite test engine used by other suites. Omitted from the ORM
        # on purpose — the live DB still enforces it via the migration's
        # literal CHECK. See ck_dp_budget_ledger_period_shape in the
        # migration.
        Index("ix_dp_budget_ledger_period", "tenant_id", "workflow_id", "period"),
    )


class DpBudgetCaps(Base):
    """FORTRESS.6.3 S1/S3 — the cap `dp_budget.reserve()`/`effective_cap()`
    check the ledger against: one row per (tenant, workflow), workflow_id
    NULL = the tenant-wide default row. Mutable (updated_at) — unlike the
    ledger this is not append-only.

    `uq_dp_budget_caps_tenant_workflow` is a UNIQUE INDEX on
    (tenant_id, COALESCE(workflow_id, 0)), not a plain UniqueConstraint —
    SQLAlchemy's declarative Table can't express a functional unique index,
    so it is intentionally omitted here; the DB (migration) is the sole
    enforcer. A duplicate tenant-wide/workflow cap row is only caught at
    flush/commit via the Postgres index, never by the ORM.
    """
    __tablename__ = "dp_budget_caps"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
    workflow_id = Column(Integer, ForeignKey("workflows.id", ondelete="RESTRICT"), nullable=True)
    epsilon_cap_per_period = Column(Numeric(12, 6), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "epsilon_cap_per_period IS NULL OR epsilon_cap_per_period > 0",
            name="ck_dp_budget_caps_cap_positive",
        ),
    )


class OutputScanExemption(Base):
    """fortress-3.6c S1 — pre-authorised output-safety exemption store.
    Matches migrations/20261012u_fortress_36c_output_scan_exemptions.py
    exactly (types, NULL/NOT NULL, FK ondelete). ``tenants`` is ON DELETE
    RESTRICT (the exemption is durable compliance evidence); every other FK
    is ON DELETE SET NULL, never CASCADE.

    Postgres-only constraints enforced by the migration but NOT reproduced
    as ORM CheckConstraint/UniqueConstraint/trigger here — same reasoning as
    DpBudgetLedger/DpBudgetCaps above: several other test suites call
    ``Base.metadata.create_all()`` against a sqlite engine, and a literal
    CHECK using Postgres-only syntax (or a functional partial UNIQUE index)
    would break those suites even though this table itself is only ever
    exercised against the real Postgres sibling DB:
      - ck_output_scan_exemptions_reason_required:
        CHECK (LENGTH(BTRIM(reason)) > 0) — BTRIM doesn't exist in SQLite.
      - ck_output_scan_exemptions_dual_control:
        CHECK (approved_by_user_id IS NULL OR created_by_user_id IS NULL OR
        approved_by_user_id <> created_by_user_id OR self_approved) — the
        maker-checker escape; approver may equal creator only when
        self_approved is true.
      - uq_output_scan_exemptions_live_key: UNIQUE INDEX on
        (tenant_id, COALESCE(workflow_id, 0), COALESCE(node_kind, ''),
        COALESCE(scanner, ''), COALESCE(finding_code, '')) WHERE
        deleted_at IS NULL — a functional partial unique index, not
        expressible as a SQLAlchemy UniqueConstraint.
      - trg_no_delete_output_scan_exemptions /
        trg_no_truncate_output_scan_exemptions: BEFORE DELETE/TRUNCATE
        triggers RAISE feature_not_supported (ISO 27001 no-hard-delete).
        Revoke by soft-deleting (set deleted_at + deleted_by_user_id)
        instead; ORM ``session.delete()`` is NOT blocked by SQLAlchemy
        itself and will only fail against the live Postgres DB at flush.
    All four are enforced solely by the migration against the real DB; the
    ORM below declares only the dialect-portable columns/FKs/index.
    """
    __tablename__ = "output_scan_exemptions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
    workflow_id = Column(Integer, ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True)
    node_kind = Column(String(250), nullable=True)
    scanner = Column(String(64), nullable=True)
    finding_code = Column(String(64), nullable=True)
    reason = Column(Text, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    self_approved = Column(Boolean, nullable=False, server_default=text("false"))
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        Index(
            "ix_output_scan_exemptions_tenant_wf",
            "tenant_id", "workflow_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class ExternalFarmConnection(Base):
    """BRIEF-EXTFARM-1 — registry row for a tenant's external orchestrator farm.

    The External Orchestrators page (routers/system_external.py) previously only
    AGGREGATED api_task_queue submit rows; there was no way to register a farm.
    This is the data layer for that registry: kind/name/endpoint are plain
    configuration, the farm's CREDENTIAL is never stored here — services/
    external_farms.py writes it to Vault KV v2 and keeps only the reference
    path in ``vault_path`` (same pattern as TenantEmailConfig.password_vault_path).

    The CheckConstraint is declared in BOTH the ORM and the migration SQL
    (20261104a_external_farm_connections.py) so a bad kind is rejected whether
    the row comes from the ORM or from raw SQL.
    """
    __tablename__ = "external_farm_connections"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    kind = Column(String(32), nullable=False)
    name = Column(String(120), nullable=False)
    endpoint = Column(String(512), nullable=False)
    # OpenBao KV v2 reference path ONLY (e.g. "system/external-farms/12"); NULL
    # when the farm was registered without a credential. Never the secret.
    vault_path = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True,
                       server_default=text("true"))
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "name",
                         name="uq_external_farm_connections_tenant_name"),
        CheckConstraint("kind IN ('kubernetes', 'qube', 'deadline')",
                        name="ck_external_farm_connections_kind"),
    )


class StorageMoveJob(Base):
    """BYO move-out (design results-1110/DESIGN-BYO-MOVE-OUT-FLOW.md s6, MIG-0) --
    `storage_move_jobs`: one row per move of a tenant's objects out of the
    house store into the tenant's own S3 bucket (a StorageConnection).

    A separate table is used on purpose: reusing ``StorageConnection.migration_*``
    would collide with migrate-on-activate on the same row. There is no new
    column on ``content_blobs`` and no blob->connection FK: "which store serves
    me" stays the active connection (``resolve_backend``).

    ``status`` is the closed vocabulary pending|running|verified|failed|cancelled
    (CHECK enforced in the migration DDL). ``tenant_id`` / ``connection_id``
    are ON DELETE SET NULL (two words) so the job record survives as history
    (ISO 27001: no hard delete; the lane code never deletes these rows).
    """
    __tablename__ = "storage_move_jobs"

    id = Column(BigInteger().with_variant(Integer, "sqlite"),
                primary_key=True, autoincrement=True)
    tenant_id = Column(ForeignKey("tenants.id", ondelete="SET NULL"),
                       nullable=True)
    connection_id = Column(ForeignKey("storage_connections.id",
                                      ondelete="SET NULL"),
                           nullable=True)
    target_bucket = Column(String, nullable=True)
    status = Column(String(16), nullable=False,
                    server_default=text("'pending'"))
    total_objects = Column(BigInteger, nullable=False,
                           server_default=text("0"))
    moved_object_count = Column(BigInteger, nullable=False,
                                server_default=text("0"))
    moved_bytes = Column(BigInteger, nullable=False,
                         server_default=text("0"))
    chunks_total = Column(Integer, nullable=False, server_default=text("0"))
    chunks_done = Column(Integer, nullable=False, server_default=text("0"))
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'verified', 'failed', "
            "'cancelled')",
            name="storage_move_jobs_status",
        ),
        Index("ix_storage_move_jobs_tenant_connection",
              "tenant_id", "connection_id"),
        Index("ix_storage_move_jobs_status", "status"),
    )


# ---------------------------------------------------------------------------
# CE-NOPAID-4.9a — the ONE paid-model-registry seam (SHARED code: this block
# ships to Paid unchanged; only CE wording lives here).
#
# CE-NOPAID-4.9c (owner answer 67, 11-13 13:57 CT: DO BEFORE LAUNCH) MOVED the
# 8 Paid compliance classes out of THIS file into the Paid add-on
# (CORE/API/paid_addon/models_paid.py, which does `from models import Base` so
# the tables stay byte-identical). The add-on lives under CORE/API/ so the
# harness syncs it and a paid image imports it as `paid_addon.models_paid`;
# the repo-root `paid-addon/` layout (CE-NOPAID-5.1) is denied by the same
# carve; CE-NOPAID-5.2 part 1 (owner D3) then moved that layout INTO this one
# add-on home (CORE/API/paid_addon/), so exactly one location holds the add-on.
# This hook is the registration half of that move: on the Paid image it
# imports the add-on and re-exports the 8 names into this module, so every
# existing `models.X` reference keeps working with ZERO edits. On CE the
# open-core carve strips the add-on dir, the import raises
# ModuleNotFoundError, and this is a NO-OP that NEVER RAISES — CE never sees a
# 500 and never a Paid table.
#
# Ordering: models.py is imported before any schema build (main.py:18
# `from database import SessionLocal, create_tables` -> main.py:254
# `create_tables()`; tests/conftest.py:393 `Base.metadata.create_all`), so a
# hook at module bottom registers the add-on tables before every create_all.
# ---------------------------------------------------------------------------
PAID_MODEL_NAMES = (
    "ComplianceAuditorGrant",
    "AccessReviewCampaign",
    "AccessReviewItem",
    "LegalHold",
    "DsarRequest",
    "LockboxRequest",
    "ComplianceAttestation",
    "ComplianceSubmissionHistory",
)


def register_paid_models() -> None:  # idempotent, never raises on CE
    """Import the Paid model add-on and re-export its classes; no-op on CE.

    Two add-on shapes are accepted: a top-level ``models_paid`` module (the
    Paid image's flattened deploy layer and the registry-hook test's Paid
    simulation) and the repo/dev-tree package ``paid_addon.models_paid``
    (``CORE/API/paid_addon/``, which the harness syncs alongside CORE/API/).
    Only a MISSING add-on counts as "this edition doesn't ship it" (same
    predicate as utils/paid_import._is_absent, plus find_spec for the
    poisoned-sys.modules simulation the CE tripwire tests use); an ImportError
    raised INSIDE a present add-on module is a real bug and re-raises.
    """
    addon = None
    try:
        import models_paid  # type: ignore
        addon = models_paid
    except ImportError as exc:
        missing = getattr(exc, "name", None) or ""
        if not missing or not (missing == "models_paid" or
                               "models_paid".startswith(missing + ".")):
            raise
    if addon is None:
        import importlib.util
        try:
            if importlib.util.find_spec("paid_addon.models_paid") is None:
                return None
        except (ImportError, ValueError, ModuleNotFoundError):
            return None  # poisoned parent (CE-carve simulation) = absent
        addon = importlib.import_module("paid_addon.models_paid")
    _g = globals()
    for _n in PAID_MODEL_NAMES:
        _g[_n] = getattr(addon, _n)
    return None


# paid-addon models register themselves here (no-op when no paid addon is installed)
# CE-NOPAID-4.9c: registration runs at models.py import time, BEFORE every
# schema build (main.py:254 create_tables, tests/conftest.py create_all), so on
# Paid the 8 add-on tables and the `models.X` names exist before anything reads
# them; on CE the carve strips the add-on and this is a no-op that never raises.
register_paid_models()

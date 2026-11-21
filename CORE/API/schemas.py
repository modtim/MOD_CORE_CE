# CORE/schemas.py

from pydantic import BaseModel, Field, EmailStr, constr, field_serializer, model_validator, field_validator, model_serializer
from typing import List, Optional, Dict, Any, Literal, Union
from datetime import datetime
from pydantic import ConfigDict

import models
from models import UserStatus, CogType, StorageType, TaskStatus, DownloadJobStatus, ResourceType

# Type aliases for permission enums (using Literal for API compatibility)
PermissionLevelType = Literal["viewer", "editor", "owner"]
PermissionTargetTypeType = Literal["cog", "workflow"]

# --- User Schemas ---
class UserBase(BaseModel):
    """Base schema with common user attributes."""
    email: Optional[EmailStr] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    onboarding_complete: Optional[bool] = False

    # LDF-3 §10 — server-side validators (single source of truth in
    # validators.identity). Run on every UserBase / UserCreate
    # submission. Empty / None usernames stay allowed at the schema
    # level so callers that update other fields without touching the
    # username don't need to repeat it; routes that REQUIRE a username
    # already check for falsy values separately.
    @field_validator("username")
    @classmethod
    def _validate_username(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        from validators.identity import validate_username, UsernameValidationError
        try:
            return validate_username(v)
        except UsernameValidationError as e:
            raise ValueError(e.message) from None

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v):
        # EmailStr already does RFC validation; we add the localhost /
        # IP-host rejection + length cap from §15.
        if v is None or v == "":
            return v
        from validators.identity import validate_email, EmailValidationError
        try:
            validate_email(str(v))
        except EmailValidationError as e:
            raise ValueError(e.message) from None
        return v

class CustomRole(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    is_system_role: bool = False
    capabilities: List['CapabilityResponse'] = []

    model_config = ConfigDict(from_attributes=True)

class User(UserBase):
    """Schema for reading user data from your database."""
    # This is a READ model. Override the inherited EmailStr type with plain str:
    # EmailStr's RFC/TLD check rejects the reserved '.local' TLD, so an
    # anonymized user (email 'deleted-N@deleted.local') would make any endpoint
    # serializing via this model raise ResponseValidationError -> HTTP 500
    # (e.g. GET /me). Inbound models (UserBase/UserCreate) keep EmailStr, so a
    # real email is still enforced on create/update. Trust the stored value.
    email: Optional[str] = None
    id: int
    keycloak_user_id: str
    tenant_id: int  # Tenant ID for multi-tenancy
    status: UserStatus
    # models.User's default=0 is Python-side only, so rows written by raw SQL
    # (smoke/bootstrap users) carry NULL; serialize those as 0 rather than 500.
    storage_attributed_bytes: int = 0
    parent_user_id: Optional[int] = None # Add parent_user_id
    custom_role: Optional[CustomRole] = None
    is_platform_admin: bool = False  # Platform admin flag
    has_custom_registry_access: bool = False  # Whether user can configure custom Docker registry
    seats_used: Optional[int] = None  # Number of active users in tenant (computed)
    seats_limit: Optional[int] = None  # Max seats allowed by plan (computed)
    storage_used_bytes: Optional[int] = None  # Current storage usage in bytes (computed)
    storage_limit_bytes: Optional[int] = None  # Max storage allowed by plan in bytes (computed)
    # GSEC FX.54 part 2 (Q37=A/Q49=A) — the effective session tier for THIS
    # caller (admin tier vs user tier), so the Dashboard/WB idle-warning dialog
    # can compute T-2 min without a second endpoint. None values = the settings
    # read failed (the dialog then falls back to the token expiry it already has).
    session_tier: Optional[Dict[str, Any]] = None
    # launch-day-followup-5 §1A — surfaced from User.email_bounce_flag.
    # True iff the Postmark bounce webhook flagged this user with ≥3
    # bounces in 30 days. Dashboard Users tab renders a warning badge.
    email_bounce_flag: bool = False

    # This is a READ model — it serializes rows already in the DB, so it must
    # NOT re-run the input-side username/email validators inherited from
    # UserBase. The deployment uses email-as-username
    # (registrationEmailAsUsername=true), so stored usernames legitimately
    # contain '@' and '.'. Re-validating on read made GET /users/ 500 the moment
    # any tenant had an email-username user (i.e. always), which broke the Users
    # page AND the share-modal recipient picker. Trust the stored value.
    @field_validator("username")
    @classmethod
    def _validate_username(cls, v):  # noqa: D401 — override to a pass-through
        return v

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v):
        return v

    @field_validator("storage_attributed_bytes", mode="before")
    @classmethod
    def _null_storage_is_zero(cls, v):
        return 0 if v is None else v

    model_config = ConfigDict(from_attributes=True)

class UserDisableResult(BaseModel):
    """FX.112.p3b — one-click disable (POST /users/{user_id}/disable).

    `user` is the soft-deactivated account; `keycloak` reports what the
    best-effort Keycloak side did (enabled=false + sign-out everywhere) —
    the DB deactivation stands even when a KC call failed, and the flags
    say so. `api_keys_revoked` = live API keys this call soft-revoked.
    """
    user: User
    api_keys_revoked: int = 0
    keycloak: Dict[str, bool] = {}


class UserSignOutEverywhereResult(BaseModel):
    """FX.112.p4b — sign-out everywhere (POST /me/signout-everywhere and
    POST /users/{user_id}/signout-everywhere). `revoked_sessions` = this
    user's session records removed (the self-service call includes the
    caller's current one); `keycloak_revoked` reports whether the
    best-effort Keycloak logout-all landed; `reauth_required` tells the
    client to re-login (the in-flight request completed, but the next
    request with the old token is dead). `user_id` is set on the
    tenant-admin variant (which user was signed out)."""
    signed_out: bool = True
    revoked_sessions: int = 0
    keycloak_revoked: bool = False
    reauth_required: bool = True
    user_id: Optional[int] = None


class UserCreate(UserBase):
    """Schema for creating a user in your DB from a token."""
    keycloak_user_id: Optional[str] = None
    parent_user_id: Optional[int] = None # Add parent_user_id


class StorageConnectionCreate(BaseModel):
    """Create a BYO-S3 storage connection. The secret key is accepted here but
    NEVER echoed back by any read model (see StorageConnection below)."""
    name: str = Field(..., min_length=1, max_length=255)
    endpoint: str = Field(..., description="S3-compatible endpoint host[:port] or URL")
    region: str = "us-east-1"
    bucket: str = Field(..., min_length=1)
    access_key: str = Field(..., min_length=1)
    secret_key: str = Field(..., min_length=1)
    secure: bool = True
    # GSEC.Q21.1 — opt in to scoped/short-lived STS session creds (default
    # False = today's static-key behaviour).
    sts_enabled: bool = False


class StorageConnectionUpdate(BaseModel):
    """Any credential/endpoint change re-arms the connection test (activation is
    re-gated) — enforced in the router. secret_key is optional (omit to keep)."""
    name: Optional[str] = None
    endpoint: Optional[str] = None
    region: Optional[str] = None
    bucket: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    secure: Optional[bool] = None
    sts_enabled: Optional[bool] = None


class StorageConnectionTestConfig(BaseModel):
    """Test an unsaved connection (UI 'Test' before Save)."""
    endpoint: str
    region: str = "us-east-1"
    bucket: str
    access_key: str
    secret_key: str
    secure: bool = True


class StorageConnection(BaseModel):
    """Read model — NEVER includes the secret key."""
    id: int
    tenant_id: Optional[int] = None
    name: str
    storage_type: StorageType
    endpoint: str
    region: str
    bucket: str
    access_key: str
    secure: bool
    # GSEC.Q21.1 — whether task containers get STS session creds for this
    # connection (opt-in; default False = static key).
    sts_enabled: bool = False
    is_active: bool
    last_test_ok: bool
    last_test_at: Optional[datetime] = None
    last_test_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # WS4.Q17.1 — migrate-on-activate status. Read-only: set by the migration
    # worker, never accepted on create/update.
    migration_status: str = "none"
    migration_started_at: Optional[datetime] = None
    migration_completed_at: Optional[datetime] = None
    migrated_object_count: int = 0
    migrated_bytes: int = 0
    migration_error: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class StorageConnectionTestResult(BaseModel):
    ok: bool
    reason: str
    cors_note: Optional[str] = None


class StorageConnectionMigrationStatus(BaseModel):
    """Read-only view of a BYO-S3 connection's migrate-on-activate copy job
    (WS4.Q17.17) — returned by GET /storage-connections/{id}/migration-status.

    The migration_* fields mirror the read-only columns on StorageConnection;
    ``total_objects`` is the manifest size of the enqueued copy task (how many
    objects the worker was asked to copy) and is null whenever no copy task
    for this connection exists (never migrated, zero-object immediate
    activation, or the task row is gone)."""
    connection_id: int
    is_active: bool
    migration_status: str
    migration_started_at: Optional[datetime] = None
    migration_completed_at: Optional[datetime] = None
    migrated_object_count: int = 0
    migrated_bytes: int = 0
    migration_error: Optional[str] = None
    total_objects: Optional[int] = None
    # FIX-BYO-MIGRATION-SKIP-MISSING — the skip report. `skipped_missing` is
    # the opt-in flag this activation ran with; `missing_source_count` is the
    # TRUE number of source keys the worker found already gone (uncapped),
    # `missing_source_keys` lists up to 50 of them. A `verified` migration
    # with skipped_missing=true and migrated_object_count < total_objects
    # means "verified, but N source objects were already gone" — the data
    # loss is reported, never hidden. Absent on old clients/never-migrated
    # connections (0/[]/false).
    skipped_missing: bool = False
    missing_source_count: int = 0
    missing_source_keys: List[str] = []


class StorageConnectionMoveOutRequest(BaseModel):
    """Body of POST /storage-connections/{id}/move-out (BYO move-out, design
    results-1110/DESIGN-BYO-MOVE-OUT-FLOW.md s3). Where the tenant's own
    objects go: MOD storage (the house bucket, the default) or another
    connection they own. Exactly one target — both at once is refused."""
    target: str = "mod_storage"
    target_connection_id: Optional[int] = None


class StorageConnectionMoveOutResult(BaseModel):
    """202 of POST /move-out: the move was armed and the copy tasks enqueued.
    ``total_objects`` is the LIST-driven manifest size (what the guard and
    the move measure), ``task_id`` the first chunk task."""
    connection_id: int
    status: str = "pending"
    total_objects: int = 0
    task_id: Optional[int] = None


class StorageConnectionMoveStatus(BaseModel):
    """GET /storage-connections/{id}/move-status: the move-out job row
    (storage_move_jobs) plus the skip report across its chunk tasks. Same
    shape of read-only bookkeeping as StorageConnectionMigrationStatus, but
    on the SEPARATE job table so a move-out never collides with a
    migrate-on-activate on the same connection row. ``move_status`` is
    'none' when no move was ever started."""
    connection_id: int
    is_active: bool
    move_status: str = "none"
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    moved_object_count: int = 0
    moved_bytes: int = 0
    total_objects: Optional[int] = None
    chunks_total: int = 0
    chunks_done: int = 0
    move_error: Optional[str] = None
    skipped_missing: bool = False
    missing_source_count: int = 0
    missing_source_keys: List[str] = []


class UserProfileUpdate(BaseModel):
    username: Optional[str] = None
    # Add other editable profile fields here

    @field_validator("username")
    @classmethod
    def _validate_username(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        from validators.identity import validate_username, UsernameValidationError
        try:
            return validate_username(v)
        except UsernameValidationError as e:
            raise ValueError(e.message) from None

class UserUpdate(BaseModel):
    custom_role_id: Optional[int] = None
    status: Optional[UserStatus] = None

# --- Cog Schemas ---
class CogBase(BaseModel):
    name: str
    cog_type: CogType = Field(
        ..., # ... indicates it's a required field
        description="The type of the Cog. Can be 'master_folder', 'asset_folder', or 'file'.\n\n"
                    "- **MASTER_FOLDER**: A top-level organizational folder, often representing a major project or category.\n"
                    "- **ASSET_FOLDER**: A sub-folder designed to contain related assets/files.\n"
                    "- **FILE**: An individual file containing actual data.\n"
                    "- **PLACEHOLDER_FILE**: A file entry that acts as a placeholder, typically to be filled by an automated workflow."
    )
    custom_metadata: Optional[Dict[str, Any]] = None
    parent_id: Optional[int] = None

class CogCreate(CogBase):
    pass

class Cog(CogBase):
    id: int
    owner_id: Optional[int] = None  # NULL for orphaned/system/wizard-standin cogs
    created_at: datetime
    data_classification: Optional[str] = 'internal'
    direct_permission_count: Optional[int] = None
    # Resolved at list/read time from the File row whose cog_id points
    # here (file-type cogs) or from the sum of descendant files
    # (folder-type cogs). Nullable because placeholder cogs and cogs
    # without an uploaded File row have no byte count.
    size_bytes: Optional[int] = None
    # Authoritative file format — sourced from ContentBlob.file_extension
    # (lowercase, no dot) and File.mime_type. These survive renames so
    # the dashboard's preview/visualizer routing can stay correct even
    # when the user renames a clip.mp4 to "demo" with no extension.
    # Populated for FILE-type cogs only; null for folders + placeholders
    # without an uploaded blob.
    file_extension: Optional[str] = None
    mime_type: Optional[str] = None
    # Human-readable original parent-folder path (e.g. "/My Files/Reports"),
    # resolved by walking the parent chain. Populated only in the Deleted
    # view (GET /cogs/?deleted=true) so the restore UI can show where a
    # soft-deleted item lived before deletion. Null for live listings and
    # for items whose parent could not be resolved.
    original_path: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class CogUpdate(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[int] = None
    custom_metadata: Optional[Dict[str, Any]] = None
    # When True, a name collision in the destination folder soft-deletes
    # the existing item before applying the rename/move. Default is
    # False — the request returns a structured 409 instead.
    replace: Optional[bool] = False

class CogClassificationUpdate(BaseModel):
    # GY.V2a: validated + canonicalized through the taxonomy (services/data_classification.py).
    # Case-insensitive; unknown labels → 422 (pydantic maps ValueError → 422).
    # Admin gating for CUI/EAR/ITAR lives in the router layer (403).
    data_classification: str = Field(..., description="Data classification level (public < internal < confidential < restricted < CUI < EAR < ITAR)")
    # GY.V2 export-control sub-tag (stored on cog.custom_metadata, NOT a
    # new column): EAR carries an optional ECCN (e.g. "3A001"); CUI an
    # optional CUI category (e.g. "SP-EXPT"). ITAR takes neither.
    export_control: Optional[dict] = Field(None, description="Optional sub-tag: {'eccn': '3A001'} for EAR or {'cui_category': 'SP-EXPT'} for CUI")

    @field_validator('data_classification')
    @classmethod
    def _validate_level(cls, v: str) -> str:
        # 1788g CE carve: services/data_classification.py is paid/denied, so the
        # taxonomy is fetched via optional_paid; CE falls back to the inline
        # canonical map below (identical vocabulary + canonical casing, so no
        # CE payload that the paid taxonomy accepts is rejected here).
        from utils.paid_import import optional_paid
        _dc = optional_paid("services.data_classification")
        if _dc is not None:
            return _dc.normalize(v)
        _canonical = {"public": "public", "internal": "internal",
                      "confidential": "confidential", "restricted": "restricted",
                      "biometric": "biometric", "cui": "CUI", "ear": "EAR",
                      "itar": "ITAR"}
        if not isinstance(v, str):
            raise ValueError("classification must be a string")
        canonical = _canonical.get(v.strip().lower())
        if canonical is None:
            raise ValueError(
                f"Unknown data classification '{v}'. Valid levels (low to high): "
                f"{', '.join(_canonical)}.")
        return canonical

    @field_validator('export_control')
    @classmethod
    def _validate_subtag(cls, v: Optional[dict]) -> Optional[dict]:
        # 1788g CE carve: this taxonomy import is dead (the body below is pure
        # structural validation, `_dc` is never referenced), so CE needs no
        # denied module here; fetched via optional_paid to keep the guard idiom
        # honest in the paid tree.
        from utils.paid_import import optional_paid
        optional_paid("services.data_classification")
        if v is None:
            return None
        if not isinstance(v, dict):
            raise ValueError("export_control sub-tag must be an object")
        out = {}
        eccn = v.get("eccn")
        if eccn is not None:
            if not isinstance(eccn, str) or not eccn.strip():
                raise ValueError("export_control.eccn must be a non-empty string")
            out["eccn"] = eccn.strip()
        cat = v.get("cui_category")
        if cat is not None:
            if not isinstance(cat, str) or not cat.strip():
                raise ValueError("export_control.cui_category must be a non-empty string")
            out["cui_category"] = cat.strip()
        return out or None

class CogCopy(BaseModel):
    destination_parent_id: Optional[int] = None
    new_name: Optional[str] = None
    # See CogUpdate.replace.
    replace: Optional[bool] = False

# --- Permission Schemas ---
class PermissionCreate(BaseModel):
    target_type: PermissionTargetTypeType  # "cog" or "workflow"
    target_id: int
    level: PermissionLevelType  # "viewer", "editor", "owner"
    user_id: Optional[int] = None
    team_id: Optional[int] = None
    keycloak_group_id: Optional[str] = None

    # Optional capability overrides (None means use default for level)
    can_upload: Optional[bool] = None
    can_create_subfolders: Optional[bool] = None
    can_move_files: Optional[bool] = None
    can_process: Optional[bool] = None
    can_share: Optional[bool] = None
    can_delete: Optional[bool] = None

    # Expiration and context for external users
    expires_at: Optional[datetime] = None
    is_external_user: bool = False
    invitation_context: Optional[Dict[str, Any]] = None

class PermissionUpdate(BaseModel):
    level: Optional[PermissionLevelType] = None
    can_upload: Optional[bool] = None
    can_create_subfolders: Optional[bool] = None
    can_move_files: Optional[bool] = None
    can_process: Optional[bool] = None
    can_share: Optional[bool] = None
    can_delete: Optional[bool] = None
    expires_at: Optional[datetime] = None

class Permission(BaseModel):
    id: int
    level: PermissionLevelType
    target_type: PermissionTargetTypeType
    target_id: int
    user_id: Optional[int] = None
    team_id: Optional[int] = None
    keycloak_group_id: Optional[str] = None

    user_email: Optional[str] = None
    user_name: Optional[str] = None
    tenant_id: Optional[int] = None
    tenant_name: Optional[str] = None

    # Effective capability values (resolved from overrides or defaults)
    can_upload: bool
    can_create_subfolders: bool
    can_move_files: bool
    can_process: bool
    can_share: bool
    can_delete: bool

    # Tracking fields
    granted_by_user_id: Optional[int] = None
    granted_at: datetime
    expires_at: Optional[datetime] = None
    is_external_user: bool
    is_expired: bool
    is_inherited: bool = False
    invitation_context: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)

# --- Workflow Schemas ---
class WorkflowVersionCreate(BaseModel):
    graph_json: dict
    # wep 3.1 — save-path `extends` (single level). The DB columns
    # extends_workflow_id / implements do not exist yet (migration k, lead), so
    # the version-create endpoint takes both as arguments, validates + resolves
    # them through services/workflow_save_inheritance.prepare_workflow_save, and
    # persists only the RESOLVED graph. `Any` (not `dict`) so a malformed
    # declaration reaches the adapter's parse_interface and is refused as
    # `invalid_interface` rather than as a pydantic field error.
    extends_workflow_id: Optional[int] = None
    implements: Optional[Any] = None

class WorkflowCreate(BaseModel):
    name: str

class Workflow(WorkflowCreate):
    id: int
    latest_version_id: Optional[int]
    # burst-to-saas §3.12 — the eligibility flag has existed on the model since
    # §3.1 but was reachable only through its own PUT, so nothing could render
    # the CURRENT state of the toggle. Read here rather than as a second
    # endpoint: the builder already loads the workflow, and a consent control
    # that has to fetch its own state can render before it knows it.
    #
    # Defaulted rather than required so a Workflow built from anything other
    # than the ORM row (imports, fixtures) does not fail validation on a field
    # that means "not permitted to leave the premises" when absent.
    burst_eligible: bool = False
    burst_enabled_by_user_id: Optional[int] = None
    burst_enabled_at: Optional[datetime] = None
    # Archive state (soft-hide). NULL/absent = visible; set = archived. Defaulted
    # so a Workflow built from a non-ORM source doesn't fail validation.
    archived_at: Optional[datetime] = None
    # GX.D1 — workflow-level default worker/pool placement
    # ({worker_id} and/or {worker_tag}); None = unpinned. Every run of this
    # workflow inherits it unless the run request or a node overrides it.
    placement: Optional[Dict[str, Any]] = None
    # GY.C7 — workflow's effective data-classification label. Read-only,
    # DERIVED at read time from the workflow's most recent run's input cogs
    # (the workflow carries no column of its own — cogs.data_classification
    # is the single source of truth). None = no labelled inputs yet. The
    # builder renders this via the shared data-label chip/banner.
    data_classification: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class WorkflowPlacementUpdate(BaseModel):
    """GX.D1 — body for PUT/GET /workflows/{id}/placement.

    `placement` may be the object itself ({"worker_id":..} / {"worker_tag":..}),
    an explicit null (clear the pin), or omitted (no change — GET-style
    partial updates). See WorkflowPlacement for the shape + precedence.
    """
    placement: Optional[Dict[str, Any]] = None
    clear: bool = False

class WorkflowVersion(BaseModel):
    id: int
    version_number: int
    graph_json: dict  # <-- Add this line
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class WorkflowNodeSummary(BaseModel):
    """Summary of a workflow node for use in NodeRun responses"""
    id: int
    graph_node_id: str
    node_kind: Optional[str] = None  # From node_definition
    cost_in_credits: int = 0  # From node_definition

    model_config = ConfigDict(from_attributes=True)

    @property
    def name(self) -> str:
        """Alias for graph_node_id to match expected field name"""
        return self.graph_node_id

class NodeRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workflow_node_id: int
    status: TaskStatus
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    inputs: Optional[Dict[str, Any]] = None
    outputs: Optional[List[Dict[str, Any]]] = None
    error_message: Optional[str] = None
    workflow_node: Optional[WorkflowNodeSummary] = None
    node_id: Optional[str] = None  # Convenience field: graph_node_id from workflow_node

    @field_serializer('status')
    def serialize_status(self, status: TaskStatus, _info):
        # Normalize for frontend: completed→success, failed→failure
        val = status.value.lower()
        if val == 'completed':
            return 'success'
        if val == 'failed':
            return 'failure'
        return val

    @model_validator(mode='after')
    def populate_node_id(self):
        """Populate node_id from workflow_node.graph_node_id if available"""
        if self.workflow_node and not self.node_id:
            self.node_id = self.workflow_node.graph_node_id
        return self

class WorkflowRunFileSummary(BaseModel):
    """File summary for workflow run listing"""
    id: int
    file_id: str
    title: str
    status: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class RunResultOutput(BaseModel):
    """One output artifact of a node in a run result (GX.R2).

    Fan-out runs carry one entry per input file — every output of every NodeRun
    of the node is included, grouped per node. ``url`` is a presigned download
    (only the first final output gets one); small text final outputs also carry
    the content inline in ``text``.
    """
    model_config = ConfigDict(extra="allow")

    cog_id: Optional[int] = None
    filename: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    url: Optional[str] = None
    text: Optional[str] = None


class RunResultError(BaseModel):
    model_config = ConfigDict(extra="allow")

    message: Optional[str] = None
    failed_node_id: Optional[str] = None
    failed_node_label: Optional[str] = None


class RunResultNode(BaseModel):
    model_config = ConfigDict(extra="allow")

    node_id: str
    label: Optional[str] = None
    status: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    outputs: List[RunResultOutput] = []


class RunFailurePrediction(BaseModel):
    """Predicted failure data returned alongside a run response (WSIP 3.4)."""
    warn: bool
    predicted_failure_probability: float


class RunResponse(BaseModel):
    """202 for a new run AND for an idempotent replay (idempotent_replay: true).
    Fields sourced from:
    - _idempotent_replay_response (routers/workflows_domains/workflows.py:1010-1015)
    - normal 202 return (lines 2572-2581)
    - capacity-queued 202 (lines 2452-2464)
    - duplicate-run 202 (lines 1913-1920).
    Status values are from models.TaskStatus (queued, running, pending_action,
    success, failure, completed, failed, blocked, suspended, awaiting_external_event)."""
    model_config = ConfigDict(extra="allow")

    workflow_run_id: int
    cog_ids: List[int] = []
    detail: Optional[str] = None
    idempotent_replay: Optional[bool] = None
    webhook_secret: Optional[str] = Field(None, description="returned once on create; never logged")
    failure_prediction: Optional[RunFailurePrediction] = None
    is_capacity_queued: Optional[bool] = None
    queue_reason: Optional[str] = None
    status: Optional[str] = None
    message: Optional[str] = None
    duplicate: Optional[bool] = None
    started_at: Optional[str] = None
    orchestrator_run_id: Optional[str] = None


class RunCancelResponse(BaseModel):
    """Modelled post-c6-J2 shape for the cancel route's post-update state.
    The cancel route itself has no return; the run row is left in status=FAILED
    with cancellation metadata in parameters."""
    model_config = ConfigDict(extra="allow")

    workflow_run_id: int
    status: str
    detail: str


class ErrorResponse(BaseModel):
    """Body of every error the c14a handlers emit (main.py:851 _handle_api_error,
    main.py:906 _handle_http_exception). Exactly the two keys those handlers can
    emit — `detail` always, `code` only when a stable snake_case code applies
    (utils/http_errors.py CODE_RE); no `error_id` here (that is the 500 handler's
    shape, main.py generic 500)."""
    model_config = ConfigDict(
        json_schema_extra={"example": {"detail": "Rate limit exceeded",
                                       "code": "rate_limited"}})

    detail: Union[str, Dict[str, Any], List[Any]] = Field(
        description="Human-readable message, or a structured validation payload "
                    "(dict/list detail passes through from HTTPException). "
                    "Callers must NOT match on this text — it is prose and changes "
                    "without notice; branch on `code` instead.")
    code: Optional[str] = Field(
        default=None, description="Stable machine-readable error code matching "
                                  "^[a-z][a-z0-9_]{2,63}$ (utils/http_errors.py:21 "
                                  "CODE_RE). Absent when no stable code applies "
                                  "(e.g. some 401s, c14d D3). This is the field "
                                  "clients branch on.")


class WebhookEventPayload(BaseModel):
    """Payload shape sent by build_payload (services/run_completion_webhooks.py:289-297).
    event must be one of run.completed / run.failed / run.cancelled."""
    event: Literal["run.completed", "run.failed", "run.cancelled"]
    run_id: int
    workflow_id: int
    status: str
    completed_at: str  # ISO-8601 UTC Z
    error: Optional[str] = None
    result_url: str


class RunUsageCost(BaseModel):
    """Customer charge for a run (30.6.10, GX.C10, CE-gating contract)."""
    model_config = ConfigDict(extra="forbid")

    amount: constr(pattern=r"^\d+\.\d{4}$") = Field(
        ..., description="customer charge in `currency`, decimal string with exactly 4 decimal places, priced at the rate in force when charged")
    currency: constr(pattern=r"^[A-Z]{3}$") = Field(
        ..., description="ISO 4217 currency code")


class RunUsage(BaseModel):
    """Per-run compute usage (GX.R2, c6-J4')."""
    model_config = ConfigDict(extra="allow")

    wall_seconds: Optional[float] = Field(
        None, description="Elapsed wall-clock seconds of the run; None until started")
    compute_seconds: float = Field(0.0, description="Sum of started_at→completed_at across all node runs (seconds)")
    nodes: int = Field(0, description="count of node runs that have started (per-node detail is the top-level `nodes` list)")
    final: bool = Field(False, description="Whether the run has reached a terminal status")
    credits_charged: Optional[int] = Field(
        None, description="omitted in CE / when no metering rows")
    cost: Optional[RunUsageCost] = Field(None, description="omitted in CE / when no metering rows")

    @model_serializer(mode="wrap")
    def serialize_usage(self, handler):
        """Drop keys whose value is None so existing callers never see
        new null fields. The wire shape stays byte-identical."""
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None}


class RunResultResponse(BaseModel):
    """GET /v1/runs/{run_id}/result — one call for an external app to get a
    finished (or in-progress) workflow run's outcome (GX.R2)."""
    model_config = ConfigDict(extra="allow")

    run_id: int
    workflow_id: Optional[int] = None
    workflow_version_id: Optional[int] = None
    status: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_s: Optional[float] = None
    error: Optional[RunResultError] = None
    nodes: List[RunResultNode] = []
    final_outputs: List[RunResultOutput] = []
    url: Optional[str] = None
    usage: Optional[RunUsage] = None


class OutputSafetyScanOverride(BaseModel):
    """POST /v1/runs/{run_id}/safety-scans/{scan_id}/override (sz-fortress-p34.4).

    A tenant user's explicit override of a `block` output-safety verdict. The
    reason is mandatory and non-trivial: the override row is an audited record
    of WHO allowed a blocked output, so a bare "ok" is not acceptable."""
    reason: str = Field(..., min_length=8, max_length=1000,
                        description="Justification for overriding the block verdict")


class OutputSafetyScanOverrideResult(BaseModel):
    """The NEW append-only override row (fortress-3.4's contract: the override
    keeps the original `block` verdict and stamps overridden_by_user_id +
    override_reason; the scanned row is never updated)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    scan_id: int
    tenant_id: int
    workflow_run_id: Optional[int] = None
    node_run_id: Optional[int] = None
    output_ref: str
    mime: Optional[str] = None
    scanner: str
    verdict: str
    findings: List[Any] = []
    overridden_by_user_id: Optional[int] = None
    override_reason: Optional[str] = None
    created_at: Optional[datetime] = None


class WorkflowRun(BaseModel):
    id: int
    run_name: Optional[str] = None
    status: str
    paused: Optional[bool] = False
    paused_nodes: Optional[List[str]] = None
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    workflow_id: Optional[int] = None
    workflow_version_id: Optional[int] = None
    workflow_name: Optional[str] = None  # Populated from relationship
    version_number: Optional[int] = None  # Populated from relationship
    orchestrator_run_id: Optional[str] = None  # For linking to workflow logs
    # Populated when the orchestrator's start-workflow-execution path
    # fails before tasks are enqueued (pending_scan node, missing
    # secret, circular dependency, etc.). Dashboard surfaces this so
    # operators can see WHY a run failed instead of just "failed".
    error_message: Optional[str] = None
    # Gate 7 — what the run cost to produce, and what it bills at. Written by
    # services/run_costing.py on completion from real task durations. NULL on
    # a run that has not finished, or one whose workers carry no cost — which
    # the UI must render as unknown rather than as free.
    cost_cents: Optional[int] = None
    billed_cents: Optional[int] = None
    # GY.C7 — the run's effective data-classification label: the highest
    # label carried by this run's input cogs (``parameters.project_info
    # .cog_ids``). Read-only, DERIVED at read time (no column); None = no
    # labelled inputs. Rendered as a data-label chip on run rows (WB RunRail,
    # Dashboard runs list).
    data_classification: Optional[str] = None
    # BUG.B27 — Dashboard "Logs → Workflow runs" rows need to be findable
    # without opening each run. DERIVED at read time (no columns added):
    # who caused the run (models.WorkflowRun.triggered_by_user_id /
    # triggered_by_trigger_id -> models.WorkflowTrigger.trigger_type), and a
    # one-line outcome summarised from the run's node_runs / error_message.
    triggered_by: Optional[str] = None
    triggered_by_user_id: Optional[int] = None
    trigger_type: Optional[str] = None
    outcome: Optional[str] = None
    # BUG.B27b — with hundreds of users a username alone does not identify who
    # ran a run (the same handle can exist in several tenants, and the owner
    # compares rows across tenants). DERIVED at read time, no new columns:
    # the run's tenant id + tenant name (models.Tenant.name) and the raw
    # username behind triggered_by_user_id (models.User.username, as opposed
    # to the human display name in `triggered_by`).
    tenant_id: Optional[int] = None
    tenant_name: Optional[str] = None
    triggered_by_username: Optional[str] = None
    node_runs: List[NodeRun] = []
    run_files: List[WorkflowRunFileSummary] = []

    model_config = ConfigDict(from_attributes=True)

class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    # FORTRESS 4.1b — the API path that sets workflows.compliance_tags.
    # Vocabulary is the platform's environment map (services/compliance_environments).
    compliance_tags: Optional[List[str]] = None
    # expr-10.1 — opt-in differentiable-workflow flag; migration 20261013k.
    differentiable: Optional[bool] = None

    @field_validator("compliance_tags")
    def _validate_compliance_tags(cls, v):
        if v is None:
            return v
        # 1788g CE carve: services/compliance_environments.py is paid/denied, so
        # the environment vocabulary is fetched via optional_paid. CE default =
        # the neutral/default environment only ("none"): the compliance profiles
        # the other tags declare are paid features, so CE refuses them rather
        # than importing the denied map. Function-level (schemas.py boots clean
        # either way).
        from utils.paid_import import optional_paid
        _cenv = optional_paid("services.compliance_environments")
        ALLOWED_ENVIRONMENTS = _cenv.ALLOWED_ENVIRONMENTS \
            if _cenv is not None else ("none",)
        if len(v) > 18:
            raise ValueError("too_many_compliance_tags:max_18")
        seen, out = set(), []
        for raw in v:
            tag = str(raw).strip().lower()
            if not tag:
                continue
            if tag not in ALLOWED_ENVIRONMENTS:
                raise ValueError(f"unknown_compliance_tag:{tag}")
            if tag not in seen:
                seen.add(tag)
                out.append(tag)
        if len(out) > 18:
            raise ValueError("too_many_compliance_tags:max_18")
        return out

# --- Workflow Export/Import Schemas ---
class WorkflowExport(BaseModel):
    """Portable workflow format for sharing between users/tenants"""
    export_version: str = "1.0"
    name: str
    description: Optional[str] = None
    graph_json: dict
    required_nodes: List[str]  # List of node_kinds needed to run this workflow

class WorkflowImport(BaseModel):
    """Request body for importing a workflow from exported JSON"""
    workflow_data: WorkflowExport
    new_name: Optional[str] = None  # Optionally rename on import

class WorkflowImportResult(BaseModel):
    """Result of workflow import operation"""
    success: bool
    workflow_id: Optional[int] = None
    workflow_name: str
    missing_nodes: List[str] = []  # node_kinds the user doesn't have access to
    message: str

# --- 30.6.9 c9-J2: workflow UPSERT-by-slug (PUT /v1/road/by-slug/{slug}) ---
class WorkflowUpsertRequest(BaseModel):
    """Body of the per-tenant slug UPSERT (c9-J2 service).

    NO tenant field anywhere: the tenant ALWAYS comes from the caller's
    argument (router auth), never from the body. extra="forbid" so a tenant
    cannot be smuggled in."""
    model_config = ConfigDict(extra="forbid")
    name: constr(min_length=1, max_length=255) = Field(description="Workflow display name (required, Q1 A)")
    graph_json: dict = Field(description="Full graph JSON {nodes:[...], edges:[...]}")
    required_nodes: List[constr(max_length=255)] = Field(default_factory=list, description="Node kinds the workflow needs (no list-level Pydantic max; the service raises too_many_required_nodes at >50)")
    description: Optional[constr(max_length=2000)] = Field(default=None, description="Optional free-form description")
    add_to_app_id: Optional[int] = Field(default=None, description="App id to add this workflow to, in the SAME transaction (collective field name)")


class WorkflowUpsertResult(BaseModel):
    """What the UPSERT actually did — every flag is a yes/no, never graph data."""
    workflow_id: int = Field(description="Id of the workflow the slug now points at")
    version_id: Optional[int] = Field(default=None, description="New version id, None when no version was written (S9)")
    created: bool = Field(default=False, description="True when the workflow row was created")
    version_created: bool = Field(default=False, description="True when a new version was appended")
    app_scope_added: bool = Field(default=False, description="True when the app scope list changed")


class SlugLookupResponse(BaseModel):
    """GET /v1/road/by-slug/{slug} payload (c9-J2 get_by_slug)."""
    workflow_id: int = Field(description="Workflow row the slug resolves to")
    slug: str = Field(description="The per-tenant slug")
    latest_version_id: Optional[int] = Field(default=None, description="Latest version row, None if unset")
    graph_hash: Optional[str] = Field(default=None, description="Canonical graph hash of the latest version (NULL on pre-migration rows)")


# --- Graph JSON schema ---
# Node kinds are free-form: every NodeDefinition kind is accepted. The tuple
# below names the graph-level primitives the platform recognises on import/save
# so a graph carrying them validates instead of being rejected. Accepting them
# here is validation only — execution behaviour ships with the phase-2+
# orchestrator work (sub_workflow_composer, agent_node_runner, workflow_memory).
EXPRESSIVENESS_NODE_KINDS = (
    "sub_workflow",
    "agent",
    "human_approval",
    "trigger",
    "memory_write_kv",
    "memory_read_kv",
    "memory_embed_and_index",
    "memory_vector_search",
    "memory_timeseries_append",
    "memory_timeseries_query",
    "retry_with_params",
    "conditional",
    "fan_out_over_list",
)


def is_expressiveness_node_kind(kind: Optional[str]) -> bool:
    """True when `kind` is a platform graph primitive rather than a NodeDefinition."""
    return bool(kind) and kind in EXPRESSIVENESS_NODE_KINDS


class GraphNodePayload(BaseModel):
    """One node inside graph_json["nodes"]."""
    id: str
    node_kind: Optional[str] = None
    node_definition_name: Optional[str] = None
    label: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    position: Optional[Dict[str, Any]] = None


class GraphNodeSchema(BaseModel):
    """graph_json envelope: nodes + edges. Kinds outside EXPRESSIVENESS_NODE_KINDS
    are not rejected — they resolve against the tenant's NodeDefinitions."""
    nodes: List[GraphNodePayload] = Field(default_factory=list)
    edges: List[Dict[str, Any]] = Field(default_factory=list)

# --- Processing Metadata Schemas (Phase 1) ---
class ProcessingHistoryEntry(BaseModel):
    """Single node execution entry in processing history"""
    node_id: str
    # Optional: None = redacted for a sealed, non-exempt buyer (GG.8b) — the
    # node name, kind, and its settings must not leak the pipeline internals.
    node_name: Optional[str] = None
    node_kind: Optional[str] = None
    status: str  # "success", "failure", "running", "skipped"
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    cost: int
    output_files: List[str] = []
    parameters: Optional[dict] = {}
    quality_metrics: dict = {}
    resource_usage: dict = {}
    intermediate_outputs: List[str] = []
    error_details: Optional[dict] = None

class WorkflowRunMetadata(BaseModel):
    """Complete workflow run metadata"""
    workflow_run_id: int
    workflow_name: str
    workflow_version: int
    executed_at: datetime
    executed_by: int
    total_cost: int
    status: str
    duration_seconds: Optional[float] = None
    nodes_executed: List[ProcessingHistoryEntry] = []
    input_files: List[str] = []
    output_files: List[str] = []

class BatchProcessingHistoryUpdate(BaseModel):
    """Request body for batch metadata updates from worker containers"""
    cog_ids: List[int]
    workflow_run_id: int
    metadata_entry: ProcessingHistoryEntry
    tenant_id: Optional[int] = None  # For service-to-service calls
    user_id: Optional[int] = None  # User who initiated the workflow

class WorkflowPlacement(BaseModel):
    """GX.D1 — pin a workflow run's tasks (or a single node's tasks) to the
    worker, or pool of workers, where the run's LLM is reachable.

    `worker_id`: one specific worker (worker_licenses.id).
    `worker_tag`: any worker whose hardware_tags include the tag.
    Both together = AND. Precedence: run request > node config > workflow
    default; a pinned task is claimable ONLY by a matching worker (enforced
    at claim time), never run elsewhere. See docs/build-on-mod/PLACEMENT.md.
    """
    worker_id: Optional[int] = None
    worker_tag: Optional[str] = None

class WorkflowRunRequest(BaseModel):
    """Request to execute workflow with multiple files.

    extra_variables allows runtime configuration of workflow nodes:
    - For Text Input nodes: pass {"text_content": "your prompt here"}
    - For processing nodes: override default values like {"texture_size": 4096}
    - Variables are matched to nodes by variable name

    A Text Input node (node_kind "text_input") can take its text inline via
    extra_variables={"text_content": "..."} — no .txt file upload is required,
    so a run whose entry node is a Text Input may pass cog_ids=[].

    Precedence (highest wins) when a variable name collides:
      1. inputs[node][var]     — explicit per-node override (see `inputs`)
      2. extra_variables[var]  — global run-level override (matched by name)
      3. the node's own configured default (node_data.extraVariables)
    """
    cog_ids: List[int]
    metadata_cog_ids: Optional[List[int]] = []
    parent_folder_id: Optional[int] = None
    refresh_token: Optional[str] = None  # Keycloak refresh token for secret access during workflow execution
    extra_variables: Optional[Dict[str, Any]] = None  # Runtime configuration variables for workflow nodes
    # GX.R3 — one-shot run-completion webhook for THIS run only (in addition
    # to any per-workflow subscription). https only unless the platform
    # setting run_webhooks.allow_http is true; private/loopback targets are
    # refused. The response returns `webhook_secret` once, for verifying the
    # signed payload (X-MOD-Signature).
    callback_url: Optional[str] = None
    # Per-node override of extra_variables. Key is the graph node id OR the
    # node's display label; value is {var: value}. Per-node values win over the
    # global extra_variables (which win over the node's configured default).
    inputs: Optional[Dict[str, Dict[str, Any]]] = None
    # workflow-agentic-execution-loop §workflow-run-execution — purpose
    # tags the run. 'production' (default) is today's behavior. New value
    # 'wizard_test_run' routes the run through the sandbox path: isolated
    # MinIO prefix `_wizard_test_runs/{wizard_session_id}/{iteration_n}/`,
    # short timeout, output-size cap, NO credit charge, per-tenant rate
    # limit. Unknown values reject 422.
    purpose: Optional[str] = "production"
    # When purpose='wizard_test_run', identifies the wizard session that
    # owns the sandbox prefix. Required for sandbox routing; ignored for
    # other purposes.
    wizard_session_id: Optional[str] = None
    # When purpose='wizard_test_run', the iteration number within the
    # session (0-indexed). Required for sandbox prefix routing.
    wizard_test_run_iteration: Optional[int] = None
    # Airflow-like concurrent runs: when True, start a NEW run even if the same
    # user already has an active (queued/running) run of this version — bypasses
    # the accidental-double-click dedup. The UI sets this for the explicit
    # "Execute new run" choice; a plain Execute leaves it False (still deduped).
    force_new: Optional[bool] = False
    # 2400GX-n8 — optional caller idempotency key: a replay with the same key
    # (scope tenant+version+key) returns the original workflow_run_id. The
    # `Idempotency-Key` header is the alternative spelling (see run handler).
    idempotency_key: Optional[str] = Field(
        None, min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._:-]+$")
    # Airflow-like "restart from node": reuse unchanged upstream node outputs via
    # the content-addressed output cache (cache-READ on for THIS run only, so
    # normal runs stay fresh). Edited/failed nodes miss the cache and re-run.
    reuse_cache: Optional[bool] = False
    # Graph node ids to FORCE re-run even on a cache hit (the "Restart from here"
    # node + everything downstream) — skips cache-read for these.
    cache_bust_nodes: Optional[List[str]] = None
    # GX.D1 — per-RUN worker/pool placement override for THIS run only
    # (see WorkflowPlacement). Wins over any node-level or workflow-level
    # placement; the named worker must belong to / be assigned to the
    # caller's tenant, else the run is rejected.
    placement: Optional["WorkflowPlacement"] = None
    # wep1.f — values for declared workflow parameters
    parameters: Optional[Dict[str, Any]] = None
    # WSIP 3.6 — bypass a predicted-failure warning; default False so the
    # gate still surfaces the warning (shadow) or blocks (enforce).
    confirm_predicted_failure: bool = False

class ProcessingHistoryResponse(BaseModel):
    """Response for processing history queries"""
    asset_id: int
    original_filename: str
    created_at: datetime
    processing_history: List[WorkflowRunMetadata]
    derived_assets: List[dict]
    total_processing_cost: int
    total_processing_time_seconds: float
    processing_count: int
    last_processed_at: Optional[datetime]
    quality_metrics: dict

class WorkflowRunFinalization(BaseModel):
    """Request body for finalizing a workflow run"""
    status: str  # "success", "failure", "partial_success"
    output_files: Optional[List[str]] = []
    error_message: Optional[str] = None
    tenant_id: Optional[int] = None  # For service-to-service calls


class WorkflowRestartRequest(BaseModel):
    """Request to restart a failed/completed workflow run"""
    resume_mode: str = Field(
        default="full",
        description="How to handle partially completed workflows:\n"
                    "- 'full': Reprocess ALL files (creates fresh run)\n"
                    "- 'failed_only': Only reprocess files that failed or never completed (skip successful ones)"
    )
    # Airflow-style "restart from node": reuse unchanged upstream outputs via the
    # output cache, and FORCE re-run these graph node ids + downstream.
    reuse_cache: Optional[bool] = False
    cache_bust_nodes: Optional[List[str]] = None

# --- Secret Schemas ---
class SecretCreate(BaseModel):
    name: str
    value: str # The raw secret, to be encrypted by the backend
    description: Optional[str] = None
    # FX.117 p4c (gov wrap) — an optional expiry at CREATE time. Ignored (and
    # allowed to stay null) on a non-gov install; under a gov/DoD compliance
    # profile it is REQUIRED and capped by secrets.gov_max_lifetime_days
    # (services/secrets_service.create_secret ->
    # services/secret_expiry_service.assert_gov_expiry, 422).
    expires_at: Optional[datetime] = None

class Secret(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    created_at: datetime
    # Whether the requesting user can actually USE this secret (read its value /
    # map it in a workflow). The list returns every tenant secret so the builder
    # can show them, but flags which ones the caller may use.
    has_access: Optional[bool] = None
    # FX.117 PART 2 (owner Q104=A) — per-secret run scope + expiry.
    # allowed_workflow_ids NULL = any workflow of the tenant; when set,
    # the run-token injection path only serves runs of these workflows.
    # expires_at NULL = no expiry; in the past, injection is refused
    # (soft state — the row is never hard-deleted).
    allowed_workflow_ids: Optional[List[int]] = None
    expires_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)

class SecretUpdate(BaseModel):
    name: Optional[str] = None
    value: Optional[str] = None
    description: Optional[str] = None
    # FX.117 PART 2 — admin/owner can set/clear the workflow scope
    # (None = unset/keep, [] would clear... None only, see service) and
    # the optional expiry.
    allowed_workflow_ids: Optional[List[int]] = None
    expires_at: Optional[datetime] = None


# --- FX.117 p3b: per-secret allowed-workflows scope ---
class SecretWorkflowScope(BaseModel):
    """GET /secrets/{id}/workflow-scope response.

    `workflow_ids` [] = unrestricted (every workflow of the tenant, today's
    behaviour). Never carries a secret value or name beyond the id echo.
    """
    secret_id: int
    tenant_id: Optional[int] = None
    workflow_ids: List[int] = Field(default_factory=list)
    unrestricted: bool = True


class SecretWorkflowScopeUpdate(BaseModel):
    """PUT /secrets/{id}/workflow-scope payload — REPLACES the whole list.

    [] clears the scope (back to "any workflow of the tenant"). Every id must
    name a workflow of the SAME tenant as the secret.
    """
    workflow_ids: List[int] = Field(default_factory=list)


class SecretWorkflowScopeResult(BaseModel):
    """PUT response: the new list plus what the replace actually changed."""
    secret_id: int
    workflow_ids: List[int] = Field(default_factory=list)
    added_workflow_ids: List[int] = Field(default_factory=list)
    removed_workflow_ids: List[int] = Field(default_factory=list)
    unrestricted: bool = True


# --- FX.117 p4a: per-secret expiry (table `secret_expiries`) ---
class SecretExpiryRead(BaseModel):
    """GET /secrets/{id}/expiry response.

    `expires_at` null = no expiry (the secret is injectable forever). Never
    carries a secret value.
    """
    secret_id: int
    tenant_id: Optional[int] = None
    expires_at: Optional[datetime] = None
    expired: bool = False


class SecretExpiryUpdate(BaseModel):
    """PUT /secrets/{id}/expiry payload — SETS or CLEARS the expiry.

    `expires_at` null clears it (the live row is soft-deleted, ISO 27001). A
    timestamp sets it; one already in the past cuts the secret off from
    run-time injection immediately.
    """
    expires_at: Optional[datetime] = None


class SecretExpiryResult(BaseModel):
    """PUT response: the new expiry plus what the write actually changed."""
    secret_id: int
    expires_at: Optional[datetime] = None
    previous_expires_at: Optional[datetime] = None
    cleared: bool = False
    expired: bool = False


# --- HF Token Schemas (GF.C0b.2) ---
class HfTokenSet(BaseModel):
    """Create/rotate payload. The value is write-only and never echoed back."""
    value: str = Field(..., min_length=8, description="HuggingFace token (e.g. hf_…)")


class HfTokenStatus(BaseModel):
    """Status read model — NEVER includes the token value, only a masked preview."""
    configured: bool
    updated_at: Optional[datetime] = None
    masked: Optional[str] = None


class HfTokenTestResult(BaseModel):
    ok: bool
    reason: str

# --- Resource Lock Schemas ---
class ResourceLockCreate(BaseModel):
    target_type: str
    target_id: int
    # Optional free-text note explaining why the resource is being locked.
    reason: Optional[str] = None
    # OPT-IN timed lock. Omit (default) for a lock that holds until explicitly
    # unlocked (no auto-expiry). A positive value acquires a lock that expires
    # after that many minutes (bounded server-side); non-positive/absent falls
    # back to the RESOURCE_LOCK_TTL_MINUTES env override, then to no-expiry.
    ttl_minutes: Optional[int] = None

class ResourceLockRenew(BaseModel):
    """Payload for renewing/extending a lock. Both fields optional: an empty
    body just pushes the expiry out by the configured TTL. `reason` (when
    provided) updates the stored note; `ttl_minutes` overrides the default
    extension window (bounded server-side)."""
    reason: Optional[str] = None
    ttl_minutes: Optional[int] = None

class ResourceLock(BaseModel):
    id: Optional[int] = None
    target_type: str
    target_id: int
    user_id: int
    tenant_id: Optional[int] = None
    reason: Optional[str] = None
    # None = the lock has no expiry and holds until explicitly unlocked.
    expires_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    renewed_at: Optional[datetime] = None
    # Holder display fields (from the ResourceLock.user relationship), so the
    # UI can show WHO holds the lock without a second lookup.
    locked_by_username: Optional[str] = None
    locked_by_email: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

# --- Tag Schemas ---
class TagBase(BaseModel):
    name: str

class TagCreate(TagBase):
    pass

class Tag(TagBase):
    id: int
    model_config = ConfigDict(from_attributes=True)

# --- Invitation Schemas ---
class InvitationCreate(BaseModel):
    email: EmailStr # Use EmailStr for validation
    custom_role_id: int
    tenant_id: Optional[int] = None  # Only platform admins can specify this; defaults to current user's tenant
    # Optional folder/file access configuration
    # If cog_id is provided, you must also provide either template_id OR permission_level
    cog_id: Optional[int] = None
    template_id: Optional[int] = None  # Use a permission template
    permission_level: Optional[models.PermissionLevel] = None  # Or specify level manually (deprecated, use templates instead)


class Invitation(BaseModel):
    id: int
    email: EmailStr
    expires_at: datetime
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


# 2400LR-BUG-1027b — CE team invites: with SMTP off the invite email never
# arrives, so the create response alone carries the accept link. SECURITY:
# this schema is used ONLY by POST /users/invitations/ (the creating admin,
# who already holds INVITATION:create). The list (GET /) and by-id/by-token
# responses stay on `Invitation` — the token/accept_link never leave the
# create response (no tokens in lists, logs or audit text).
class InvitationCreated(Invitation):
    accept_link: Optional[str] = None
    # FIX-INVITATIONS-EMAIL-DELIVERED-FIELD (REV 7ffe9dff26 option 1): the create
    # response carried NO delivery signal, so the Dashboard could only guess "was an
    # email sent?" from accept_link + entitlements and lied on CE-with-mail-configured
    # and on paid with an unreadable /platform/features payload. Additive field:
    # True = the invitation email was queued, False = nothing could be sent,
    # None = default (every older producer/consumer unchanged; the Dashboard treats
    # only a STRICT false as "no email").
    email_delivered: Optional[bool] = None

# --- Team Schemas ---
class TeamCreate(BaseModel):
    name: str

class Team(BaseModel):
    id: int
    name: str
    model_config = ConfigDict(from_attributes=True)

class TeamUpdate(BaseModel):
    name: Optional[str] = None

# --- Manual Intervention Schema ---
class ManualIntervention(BaseModel):
    id: int
    node_run_id: int
    status: str
    instructions: str
    assigned_at: datetime
    model_config = ConfigDict(from_attributes=True)

# --- Shareable Link Schemas ---
class ShareableLinkCreate(BaseModel):
    permission_level: models.PermissionLevel | PermissionLevelType = "viewer"  # enum from model / string from client
    expires_at: Optional[datetime] = None
    password: Optional[str] = None

class ShareableLink(ShareableLinkCreate):
    id: int
    token: str
    created_at: Optional[datetime] = None
    last_visited_at: Optional[datetime] = None
    # FX.102 — the visit/download counters the share policy surfaces in
    # the admin list (download_count drives the share.download_cap gate).
    visit_count: int = 0
    download_count: int = 0
    model_config = ConfigDict(from_attributes=True)

# --- Billing and Subscription Schemas ---
class Plan(BaseModel):
    id: int
    name: str
    price_in_cents: int
    # `None` = unlimited (matches PlanCreate semantics — see that
    # class's docstring). `0` = real zero cap (used for soft-suspended
    # tenants). Without `Optional[int]`, serializing a Platform/custom
    # unlimited plan crashes with ResponseValidationError because the
    # DB column is nullable.
    # Response shape — DB columns are NOT NULL post-migration. Kept
    # Optional[int] in the wire schema only so legacy callers / tests
    # that don't supply the field still validate cleanly; new data
    # will always have ints.
    max_seats: Optional[int] = None
    max_storage_gb: Optional[int] = None
    credits_per_month: Optional[int] = None
    max_concurrent_processes: Optional[int] = None
    is_free_tier: bool
    is_popular: bool
    is_custom_plan: bool
    # `None` on a custom plan = "template" not yet bound to a tenant.
    # Admins can create a template once and hand it to multiple tenants
    # via /admin/plans/{id}/assign/{tenant_id} (contract negotiated
    # once, applied across the same customer's tenants). The legacy
    # form sets this at create-time; the new form leaves it null and
    # uses the Assign flow per-tenant.
    custom_for_tenant_id: Optional[int] = None
    custom_description: Optional[str] = None
    display_order: int
    stripe_price_id: Optional[str] = None  # Monthly Price ID
    stripe_price_id_annual: Optional[str] = None  # Annual Price ID
    allow_custom_registry: bool = False
    overage_credit_price_cents: Optional[int] = None
    included_egress_gb: Optional[int] = None
    egress_overage_price_cents: Optional[int] = None
    # DERP relay / bandwidth (tailnet-plan Group 8). See models.Plan for the
    # column-level docs. Optional in the wire schema for the same legacy/test
    # back-compat reason as the egress fields above.
    included_relay_gb: Optional[int] = None
    relay_overage_price_cents: Optional[int] = None
    relay_entitled: Optional[bool] = None
    free_trial_days: int = 0
    # Identity & Access feature gates (per plan). Default false
    # so legacy rows / tests without the column still validate.
    sso_enabled: bool = False
    scim_enabled: bool = False
    # clean-v2-api Wave 10 / GT.D12 — the plan's commercial-tier bundle
    # (a tier name utils.entitlements defines). `None` on a row that predates
    # tiers; `tier_missing` flags those so the admin UI can surface them.
    tier: Optional[str] = None
    tier_missing: bool = False
    model_config = ConfigDict(from_attributes=True)

class PlanPublic(BaseModel):
    """Public plan info for pricing page"""
    id: int
    name: str
    price_in_cents: int
    # See `Plan.max_seats` comment — `None` = unlimited, `0` = zero cap.
    max_seats: Optional[int] = None
    max_storage_gb: Optional[int] = None
    credits_per_month: Optional[int] = None
    max_concurrent_processes: Optional[int] = None
    is_free_tier: bool
    is_popular: bool
    display_order: int
    stripe_price_id: Optional[str] = None  # Monthly Price ID for Stripe Checkout
    stripe_price_id_annual: Optional[str] = None  # Annual Price ID (sibling — same plan)
    allow_custom_registry: bool = False
    overage_credit_price_cents: Optional[int] = None
    included_egress_gb: Optional[int] = None
    egress_overage_price_cents: Optional[int] = None
    free_trial_days: int = 0
    model_config = ConfigDict(from_attributes=True)

class PlanCreate(BaseModel):
    """Schema for creating a plan (admin only). Used for both standard
    public plans and custom-per-tenant plans (#15).

    Limit-field conventions for `max_seats`, `max_storage_gb`,
    `max_concurrent_processes`:
      - `null` (omit or `None`) → unlimited. Enforced as a short-circuit
        in utils/usage_enforcement.py + utils/storage_limits.py.
      - `0` → zero-allowance cap. Useful for soft-suspending a tenant
        (block all activity without canceling the subscription).
      - `N` (positive) → cap at N.

    `credits_per_month` is required (non-null). 0 means "no included
    credits, all overage" — used by unlimited plans. Type a large
    number for an effectively-unlimited credit plan.

    Integer ceilings (`le=...`) cap inputs well under Postgres int4
    (max signed = 2_147_483_647). Anything above bounces with a clean
    422 instead of crashing the INSERT with "integer out of range".
    """
    name: str
    # Cap fields are required ints (no NULL accepted on the API surface).
    # `0` = zero allowance / blocked (soft-suspend a tenant).
    # `N > 0` = N is the cap.
    # `1_000_000_000_000_000` (1 quadrillion, the UNLIMITED_SENTINEL) =
    # effectively unlimited. Backing columns are BIGINT post-migration
    # so this sentinel + any reasonable usage counter can never overflow.
    max_seats: int = Field(ge=0, le=1_000_000_000_000_000, description="0 = blocked, 1Q = unlimited, N = cap")
    max_storage_gb: int = Field(ge=0, le=1_000_000_000_000_000, description="0 = blocked, 1Q = unlimited, GB cap")
    credits_per_month: int = Field(ge=0, le=1_000_000_000_000_000, description="0 = none included (overage-only), 1Q = effectively unlimited, N = included credits/month")
    max_concurrent_processes: int = Field(ge=0, le=1_000_000_000_000_000, description="0 = blocked, 1Q = unlimited, N = cap")
    price_in_cents: int = Field(ge=0, le=2_000_000_000, description="Price in cents.")
    # Egress / bandwidth (add-egress-billing). Optional on input for back-compat
    # with existing create calls; default to no allowance + no overage pricing.
    included_egress_gb: int = Field(default=0, ge=0, le=1_000_000_000_000_000, description="Monthly egress (bytes-out) allowance in GB. 0 = none included.")
    egress_overage_price_cents: Optional[int] = Field(default=None, ge=0, le=2_000_000_000, description="Per-GB egress overage price in cents. null = egress not billed.")
    # DERP relay / bandwidth (tailnet-plan Group 8). Optional on input, same
    # back-compat convention as egress above; None -> the model-level column
    # default (0 / null / false) applies.
    included_relay_gb: Optional[int] = Field(default=None, ge=0, le=1_000_000_000_000_000, description="Monthly MOD-relayed-bytes allowance in GB. null/0 = none included.")
    relay_overage_price_cents: Optional[int] = Field(default=None, ge=0, le=2_000_000_000, description="Per-GB relay overage price in cents. null = relay overage not billed.")
    relay_entitled: Optional[bool] = Field(default=None, description="Whether this entitlement may use MOD's DERP relay at all. null/false = not entitled.")
    allow_custom_registry: bool = False
    custom_for_tenant_id: Optional[int] = None
    custom_description: Optional[str] = None
    # Stripe binding (set after creating the matching Price in Stripe).
    # Without it the plan is "pay-as-you-go" / contact-sales only.
    stripe_price_id: Optional[str] = None
    is_popular: Optional[bool] = False
    display_order: Optional[int] = None
    # Identity & Access feature gates. Let a platform admin stand up a custom
    # plan with SSO/SCIM included at create-time. Default off.
    sso_enabled: Optional[bool] = False
    scim_enabled: Optional[bool] = False
    # GT.D12 — the commercial-tier capability bundle is REQUIRED on create.
    # One of utils.entitlements.CANONICAL_TIERS; anything else is a 422.
    tier: str = Field(description="Tier bundle (a tier the entitlements module defines)")

    @field_validator("tier")
    @classmethod
    def _validate_tier(cls, v: Optional[str]) -> Optional[str]:
        from utils.entitlements import CANONICAL_TIERS
        if v is None or v not in CANONICAL_TIERS:
            raise ValueError(
                f"tier must be one of {list(CANONICAL_TIERS)} (got {v!r})")
        return v

class PlanUpdate(BaseModel):
    """Schema for updating a plan (admin only)."""
    name: Optional[str] = None
    max_seats: Optional[int] = None
    max_storage_gb: Optional[int] = None
    credits_per_month: Optional[int] = None
    max_concurrent_processes: Optional[int] = None
    price_in_cents: Optional[int] = None
    included_egress_gb: Optional[int] = None
    egress_overage_price_cents: Optional[int] = None
    # DERP relay / bandwidth (tailnet-plan Group 8). PATCH applies these
    # generically via model_dump(exclude_unset=True) + setattr, same as the
    # egress fields above.
    included_relay_gb: Optional[int] = None
    relay_overage_price_cents: Optional[int] = None
    relay_entitled: Optional[bool] = None
    allow_custom_registry: Optional[bool] = None
    custom_description: Optional[str] = None
    stripe_price_id: Optional[str] = None
    is_popular: Optional[bool] = None
    is_visible: Optional[bool] = None
    display_order: Optional[int] = None
    # Identity & Access feature gates — the per-plan toggle a platform admin
    # flips to add SSO/SCIM to any (incl. custom) plan. PATCH applies these
    # generically via model_dump(exclude_unset=True).
    sso_enabled: Optional[bool] = None
    scim_enabled: Optional[bool] = None
    # GT.D12 — optional on PATCH (partial update; omitting it leaves the
    # current tier alone, incl. legacy NULL-tier plans awaiting the tier
    # migration), but when provided it MUST be a canonical tier or the save
    # is rejected with 422.
    tier: Optional[str] = None

    @field_validator("tier")
    @classmethod
    def _validate_tier(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        from utils.entitlements import CANONICAL_TIERS
        if v not in CANONICAL_TIERS:
            raise ValueError(
                f"tier must be one of {list(CANONICAL_TIERS)} (got {v!r})")
        return v

class Subscription(BaseModel):
    status: str
    is_trial: bool
    trial_ends_at: Optional[datetime] = None
    current_period_start: Optional[datetime] = None
    current_period_ends_at: Optional[datetime] = None
    plan: Plan
    model_config = ConfigDict(from_attributes=True)

class SubscriptionWithUsage(Subscription):
    """Subscription with current usage stats"""
    credits_remaining: int
    # OWNER-DECISIONS 09-29 — the never-expiring purchased-credits pool
    # (one-time top-ups), additive to `credits_remaining` (the plan's
    # per-period allocation). Additive field; defaults to 0 for older
    # callers/tenants with none.
    purchased_credits_remaining: int = 0
    credits_total: int
    credits_used: int
    storage_used_bytes: int
    # Harbor (node-image) storage — surfaced in the usage widget alongside MinIO
    # file storage. Defaults to 0 so older callers/tenants without a snapshot row
    # still validate.
    harbor_used_bytes: int = 0
    # None = unlimited (Platform plan / Enterprise unlimited tiers).
    storage_limit_bytes: Optional[int] = None
    seats_used: int
    seats_limit: int
    concurrent_processes_running: int
    concurrent_processes_limit: int
    credit_advance_available: int  # How much can be borrowed from next period
    # Egress (add-egress-billing). limit None = unlimited (platform); price None =
    # not billed. Bytes are decimal (10^9 GB) to match the billing unit.
    egress_used_bytes: int = 0
    egress_included_gb: Optional[int] = None
    egress_limit_bytes: Optional[int] = None
    egress_overage_price_cents: Optional[int] = None
    # GSEC FX.41 (Q23=A) — set (non-None) only while the tenant is over a
    # downgraded plan limit. The Dashboard renders an in-app banner from this;
    # each listed item says what is over and by how much. None = under every
    # limit (normal operation, no banner).
    overage_banner: Optional[dict] = None

class PortalSession(BaseModel):
    portal_url: str

class CheckoutSession(BaseModel):
    checkout_url: str
    session_id: str


# --- Plan Change Schemas (#101) ---
class ChangePlanRequest(BaseModel):
    """Request to change subscription plan."""
    plan_id: int = Field(..., description="ID of the entitlement to switch to")


class ChangePlanResponse(BaseModel):
    """Response after changing subscription plan."""
    success: bool
    message: str
    change_type: str  # 'upgrade' or 'downgrade'
    old_plan_name: str
    new_plan_name: str
    effective_immediately: bool
    scheduled_at: Optional[datetime] = None
    prorated_credits_added: int = 0
    new_credits_remaining: Optional[int] = None


# --- Plan Comparison Schemas (#105) ---
class PlanCompareItem(BaseModel):
    """Single plan for comparison page."""
    id: int
    name: str
    price_in_cents: int
    credits_per_month: int
    max_seats: int
    # None = unlimited storage.
    max_storage_gb: Optional[int] = None
    max_concurrent_processes: int
    is_free_tier: bool
    is_popular: bool
    display_order: int
    allow_custom_registry: bool = False
    overage_credit_price_cents: Optional[int] = None
    free_trial_days: int = 0
    features: List[str]
    model_config = ConfigDict(from_attributes=True)


class PlanCompareResponse(BaseModel):
    """Response for plan comparison endpoint."""
    plans: List[PlanCompareItem]


# --- Trial Status Schemas (#106) ---
class TrialStatusResponse(BaseModel):
    """Response for trial status check."""
    is_trial: bool
    trial_ends_at: Optional[datetime] = None
    days_remaining: int = 0
    has_payment_method: bool = False
    plan_name: Optional[str] = None


# --- Credit Management Schemas ---
class CreditAdjustment(BaseModel):
    """Schema for manual credit adjustments by admin"""
    amount: int = Field(..., description="Amount to add (positive) or deduct (negative) from tenant's credit balance")
    reason: str = Field(..., description="Reason for the credit adjustment (required for audit trail)")

class CreditLedgerEntryResponse(BaseModel):
    """Schema for returning credit ledger history"""
    id: int
    tenant_id: int
    workflow_run_id: Optional[int] = None
    user_id: Optional[int] = None
    entry_type: str  # CREDIT_PURCHASE, WORKFLOW_EXECUTION, REFUND, ADJUSTMENT
    amount_in_credits: int
    balance_after: int
    notes: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

# --- API Key Schemas ---
class ApiKeyBase(BaseModel):
    description: Optional[str] = Field(None, description="A description to help identify the key's purpose.")

class ApiKeyCreate(ApiKeyBase):
    expires_at: Optional[datetime] = Field(None, description="Optional future expiration date for the key.")
    capability_scope: Optional[List[str]] = Field(
        None,
        description=(
            "Optional allowlist of capability scopes this key is limited to "
            "(e.g. ['node:author']). Omit for a full-access key."
        ),
    )
    is_read_only: Optional[bool] = Field(
        None,
        description="When true, the key may only perform non-mutating operations.",
    )
    stack_id: Optional[int] = Field(
        None,
        description=(
            "Stack-pinned key (formerly deploy token): the persistent stack this "
            "key may act on. Requires a stack:* capability_scope and the caller "
            "holding the stack:author capability."
        ),
    )

    @field_validator("expires_at")
    @classmethod
    def _validate_expires_at(cls, v: Optional[datetime]) -> Optional[datetime]:
        # GY.C3a — a caller-supplied expiry beyond the platform ceiling
        # (auth.api_key_max_ttl_days > 0) is refused up front with a clear
        # message (the router re-enforces for both this case and the
        # missing-expiry case).
        if v is None:
            return v
        from datetime import timezone as _tz, timedelta as _td

        from services import platform_settings as psr
        ceiling = int(psr.get_setting(
            "auth.api_key_max_ttl_days") or 0)
        if ceiling <= 0:
            return v
        if v.tzinfo is None:
            v = v.replace(tzinfo=_tz.utc)
        if v > datetime.now(_tz.utc) + _td(days=ceiling):
            raise ValueError(
                f"expires_at is beyond the platform ceiling of {ceiling} "
                f"days (auth.api_key_max_ttl_days); set it within that "
                f"window")
        return v

    @field_validator("capability_scope")
    @classmethod
    def _validate_capability_scope(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        from utils.api_key_scopes import is_valid_scope
        # 30.6.3 tok-merge (J2) — the stack-key scopes (stack:deploy|logs|
        # backups:read|backups:run|seed) carry TWO colons for two of the names,
        # which the `resource:capability` regex would reject; skip ONLY exact
        # known stack-scope entries here (they are validated by
        # services/app_key_scope.validate_stack_key_request in the router, which
        # answers a clear 422). Everything else — including the platform
        # capability `stack:author` — keeps going through is_valid_scope exactly
        # as today.
        from services.app_key_scope import (
            STACK_KEY_SCOPES, STACK_KEY_SCOPE_PREFIX)
        known_stack = {STACK_KEY_SCOPE_PREFIX + n for n in STACK_KEY_SCOPES}
        bad = [s for s in v if s not in known_stack and not is_valid_scope(s)]
        if bad:
            raise ValueError(
                f"Invalid capability scope(s): {bad}. Use a known scope "
                f"(e.g. 'node:author') or a 'resource:capability' pair."
            )
        return v

class ApiKey(ApiKeyBase):
    id: int
    key_prefix: str
    last_used_at: Optional[datetime] = None
    created_at: datetime
    # 30.6.3 tok-merge (J2) — a stack-pinned key (formerly deploy token): the
    # pin comes from the row column `deploy_token_stack_id` (mapped explicitly
    # by routers/api_keys.read_api_keys) and `stack_name` is resolved there by
    # ONE tenant-scoped IN query on PersistentStack (never a column, never a
    # cross-tenant name).
    stack_id: Optional[int] = None
    stack_name: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class NewApiKey(ApiKey):
    access_key: str = Field(..., description="The new, full API key. This is the only time it will be shown.")


class ApiKeyTokenRequest(BaseModel):
    """Request to exchange an API key for an OAuth token."""
    api_key: str = Field(..., description="The full API key (sk_xxx...)")


class ApiKeyTokenResponse(BaseModel):
    """
    OAuth token response from API key exchange.

    CMMC/ISO 27001 Compliance:
    - Access tokens are short-lived (default 1 hour)
    - Tokens are standard OAuth 2.0 JWTs
    - Full audit trail via Keycloak
    """
    access_token: str = Field(..., description="OAuth 2.0 access token (JWT)")
    token_type: str = Field(default="Bearer", description="Token type, always 'Bearer'")
    expires_in: int = Field(..., description="Token lifetime in seconds")
    scope: Optional[str] = Field(None, description="Granted scopes")
    # GSEC R-020 — the minted token carries the SOURCE key's scope so the
    # caller (and any token consumer) knows the effective permissions. These
    # are the key's is_read_only flag + capability allowlist, surfaced here
    # because Keycloak's client-credentials token does not itself embed them.
    is_read_only: bool = Field(False, description="The source key is read-only")
    capability_scope: Optional[list] = Field(
        None, description="The source key's capability allowlist (null = unscoped)")


# --- Platform Payment Settings Schemas ---
class PlatformPaymentSettingsUpdate(BaseModel):
    """
    Update platform payment credentials (Stripe).

    CMMC/ISO 27001: SC-13 - All credentials encrypted at rest.
    Only platform admins can update these settings.
    """
    secret_key: Optional[str] = Field(None, description="Provider secret key (stored via Vault)")
    publishable_key: Optional[str] = Field(None, description="Provider publishable key (stored via Vault)")
    webhook_secret: Optional[str] = Field(None, description="Provider webhook signing secret")
    is_test_mode: Optional[bool] = Field(None, description="True for test keys, False for live keys")


class PlatformPaymentSettingsResponse(BaseModel):
    """Platform payment settings (credentials masked for security)."""
    provider: str
    is_configured: bool
    is_test_mode: bool
    # Masked versions of keys (only show first/last few chars)
    secret_key_hint: Optional[str] = None  # e.g., "sk_test_...abc"
    publishable_key_hint: Optional[str] = None  # e.g., "pk_test_...xyz"
    webhook_secret_configured: bool = False
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# --- Platform Email Config Schemas (launch-day-followup-5 §1A) ---
class PlatformEmailConfigUpdate(BaseModel):
    """
    Update platform email provider configuration.

    Used by POST /admin/email-config. Platform-admin only. Operates on
    the tenant_id=1 row of tenant_email_configs (the platform-wide
    config that all tenants fall back to in SaaS mode).

    `smtp_password` semantics:
      - None (field omitted)  → preserve the existing Vault value.
      - "" (empty string)     → clear the password / unconfigure.
      - non-empty string      → write to Vault, clear the legacy
                                encrypted_smtp_password column.
    """
    provider: Optional[str] = Field(None, description="Provider kind: 'custom', 'postmark', 'brevo', 'mailgun'")
    smtp_host: Optional[str] = Field(None, description="SMTP server hostname (e.g. smtp.postmarkapp.com)")
    smtp_port: Optional[int] = Field(None, description="SMTP port (587 STARTTLS, 465 SSL)")
    smtp_user: Optional[str] = Field(None, description="SMTP username (Postmark = Server API token)")
    smtp_password: Optional[str] = Field(None, description="SMTP password. None=preserve, ''=clear, value=write")
    sender_name: Optional[str] = Field(None, description="From-name shown in recipient inbox")
    sender_email: Optional[EmailStr] = Field(None, description="From-address (must match verified domain)")
    smtp_security: Optional[str] = Field(
        None,
        description="Transport encryption: 'auto' (guess from the port), 'starttls', 'ssl' or 'none'. None=preserve",
    )
    smtp_allow_selfsigned: Optional[bool] = Field(
        None,
        description="Accept a self-signed / private-CA server certificate (skips verification). None=preserve",
    )

    @field_validator("smtp_security")
    @classmethod
    def _validate_smtp_security(cls, v):
        if v is None:
            return v
        from utils.smtp_security import normalize_security
        # 'auto' is stored as NULL; anything else outside the set -> 422.
        return normalize_security(v) or "auto"


class PlatformEmailConfigResponse(BaseModel):
    """Platform email config (password never returned, only flags)."""
    provider: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    sender_name: Optional[str] = None
    sender_email: Optional[str] = None
    smtp_security: str = "auto"        # 'auto' | 'starttls' | 'ssl' | 'none'
    smtp_allow_selfsigned: bool = False
    is_configured: bool = False        # has provider + host + user + password
    password_set: bool = False         # password exists in Vault (or legacy encrypted col)
    last_tested_at: Optional[datetime] = None
    last_test_result: Optional[str] = None  # 'success' / 'failed' / None
    last_test_error: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class EmailTestRequest(BaseModel):
    """Send-test-email payload."""
    recipient_email: EmailStr = Field(..., description="Address to send the test message to")


class EmailTestResponse(BaseModel):
    """Send-test-email result."""
    success: bool
    provider_message_id: Optional[str] = None  # Postmark/SMTP server response id when available
    error: Optional[str] = None  # specific failure reason on success=False


class StorageWarning(BaseModel):
    """A single storage health warning surfaced to the platform admin."""
    level: str = Field(..., description="critical | warning | info")
    code: str = Field(..., description="stable machine code, e.g. network_filesystem | disk_low")
    message: str = Field(..., description="human-facing explanation + remedy")


class PlatformStorageStatusResponse(BaseModel):
    """Read-only platform storage status — surface for the Settings →
    Storage panel. Exposes where the object store (versitygw) writes its
    data (the host path bound to /data inside the container) plus the
    underlying filesystem's type and current usage. Set by
    `start-main-server.sh` via the `STORAGE_DATA_PATH` env var
    (`S3_DATA_PATH` read first, `MINIO_DATA_PATH` as legacy fallback); see launch-day-followup-6 §1.
    """
    data_path: str = Field(..., description="Host path bound to the object store's /data volume")
    filesystem_type: Optional[str] = Field(None, description="Filesystem type at data_path (e.g. ext4, nfs4, cifs)")
    bytes_total: int = Field(..., description="Total bytes on the filesystem hosting data_path")
    bytes_used: int = Field(..., description="Bytes currently used")
    bytes_free: int = Field(..., description="Bytes free for new files")
    last_refreshed_at: datetime = Field(..., description="UTC timestamp when these stats were measured")
    warnings: List[StorageWarning] = Field(default_factory=list, description="Storage health warnings (network-FS, low space, …)")


# --- Version Schemas ---
# NOTE: Version records now link to ContentBlob via content_id
# The content_id is a SHA256 hash that references the ContentBlob table
# storage_path is stored in ContentBlob, not Version
class VersionBase(BaseModel):
    version_number: int
    content_id: str  # SHA256 hash linking to ContentBlob.id

class VersionCreate(VersionBase):
    tenant_id: int  # Required for ContentBlob FK constraint

class Version(VersionBase):
    id: int
    cog_id: int
    tenant_id: int
    name: Optional[str] = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

# --- Webhook Schemas ---
class WebhookEndpointBase(BaseModel):
    url: str
    description: Optional[str] = None
    subscribed_events: List[str] = []
    is_active: bool = True

class WebhookEndpointCreate(WebhookEndpointBase):
    pass

class WebhookEndpoint(WebhookEndpointBase):
    id: int
    tenant_id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class WebhookEndpointCreated(WebhookEndpoint):
    """BUG.B36b — the 201 create response ONLY. Carries the generated signing
    secret under the key the Dashboard one-time key modal reads (`secret`), so
    the UI can show it exactly once. The ORM attribute is `signing_secret`, so
    the create route maps it explicitly (`secret=row.signing_secret`); `from_attributes`
    would not auto-map the two names. The list/get models (WebhookEndpoint)
    stay unchanged — the secret never reaches a list or get-one response."""
    secret: str  # shown ONCE — only this response carries it


# --- GX.R3: Run-completion webhook subscriptions ---------------------------
class RunWebhookSubscriptionCreate(BaseModel):
    url: str
    description: Optional[str] = None


class RunWebhookSubscriptionOut(BaseModel):
    """Subscription row (secret NEVER included — it is shown once at create /
    rotate via the dedicated `secret` field on those responses)."""
    id: int
    tenant_id: int
    workflow_id: int
    url: str
    is_active: bool
    description: Optional[str] = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class RunWebhookSubscriptionCreated(RunWebhookSubscriptionOut):
    secret: str  # shown ONCE — only this response carries it


class RunWebhookSecretRotated(BaseModel):
    id: int
    secret: str  # new signing secret, shown ONCE


class RunWebhookDeliveryOut(BaseModel):
    id: int
    tenant_id: int
    workflow_id: Optional[int] = None
    subscription_id: Optional[int] = None
    run_id: Optional[int] = None
    url: str
    event: str
    delivery_uuid: str
    status: str
    attempts: int
    last_attempted_at: Optional[datetime] = None
    last_http_status: Optional[int] = None
    last_error: Optional[str] = None
    next_attempt_at: Optional[datetime] = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class RunWebhookDeliveryAttemptOut(BaseModel):
    id: int
    delivery_id: int
    attempt_number: int
    outcome: str
    http_status: Optional[int] = None
    error: Optional[str] = None
    attempted_at: datetime
    model_config = ConfigDict(from_attributes=True)


# --- Auth Schemas ---
class TokenData(BaseModel):
    keycloak_user_id: str | None = None

# --- Custom Role Schemas ---
class CustomRoleBase(BaseModel):
    name: str
    description: Optional[str] = None

class CustomRoleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    capability_ids: List[int] = []

class CustomRoleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    capability_ids: Optional[List[int]] = None


class ContentBlobBase(BaseModel):
    size_bytes: int
    mime_type: str
    storage_path: str
    storage_type: StorageType = StorageType.MINIO # Default to MINIO
    model_config = ConfigDict(arbitrary_types_allowed=True)

class ContentBlobCreate(ContentBlobBase):
    pass

class ContentBlob(ContentBlobBase):
    id: str # UUID will be represented as a string
    cog_id: Optional[int] = None # Can be None initially
    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)


# launch-completion-megaplan GP.4 / D-P1 — native recipe schema.
#
# A node's `custom_metadata` may declare `execution: native` plus a per-OS
# recipe (`native.platforms.{windows,darwin,linux}.{command, args_template,
# env}`) and an optional `requires_app {name, min_version, detect}`.
# Execution is NEVER inferred: the fields require the explicit flag, and the
# flag requires at least one platform recipe — this validator enforces both
# directions with clear 422 errors rather than silently accepting a
# half-specified recipe that only fails at claim time.
_NATIVE_RECIPE_OS_KEYS = {"windows", "darwin", "linux"}
_NATIVE_RECIPE_DETECT_KEYS = {"windows_registry", "path", "bundle_id"}


def _validate_native_recipe_metadata(custom_metadata):
    """Validate the D-P1 native recipe shape inside `custom_metadata`.

    Returns the (unmodified) value on success; raises ValueError with a
    clear, specific message otherwise. `ValueError` inside a Pydantic
    validator surfaces as a normal 422 field error.
    """
    if not isinstance(custom_metadata, dict):
        return custom_metadata

    execution = custom_metadata.get("execution")
    has_native_block = "native" in custom_metadata
    has_requires_app = "requires_app" in custom_metadata

    if execution != "native":
        if has_native_block or has_requires_app:
            raise ValueError(
                "custom_metadata.native / requires_app require "
                "execution: native — execution is never inferred."
            )
        return custom_metadata

    native = custom_metadata.get("native")
    if not isinstance(native, dict) or not isinstance(native.get("platforms"), dict) \
            or not native.get("platforms"):
        raise ValueError(
            "execution: native requires native.platforms with a recipe for "
            "at least one of windows, darwin, linux."
        )

    platforms = native["platforms"]
    known = {k: v for k, v in platforms.items() if k in _NATIVE_RECIPE_OS_KEYS}
    if not known:
        raise ValueError(
            "native.platforms must declare at least one of "
            f"{sorted(_NATIVE_RECIPE_OS_KEYS)}; got keys {sorted(platforms.keys())}."
        )
    def _check_requires_app(requires_app, where="requires_app"):
        """One requires_app requirement (node-level or per-OS, GP.16c).

        The node-level form is a `{name, min_version, detect}` object. A
        per-OS entry may also be an EXPLICIT EMPTY LIST — the documented
        "needs nothing on this OS" marker (presence of the key is what
        overrides the node-level value) — which is valid. An empty dict /
        anything else without a name is rejected, mirroring the node-level
        rules."""
        if isinstance(requires_app, list):
            if requires_app:
                raise ValueError(
                    f"{where} must be an object (or an explicit empty list "
                    "to mean 'needs nothing on this OS'), not a non-empty list."
                )
            return
        if not isinstance(requires_app, dict) or not str(requires_app.get("name") or "").strip():
            raise ValueError(f"{where}.name is required when {where} is set.")
        min_version = requires_app.get("min_version")
        if min_version is not None and not isinstance(min_version, str):
            raise ValueError(f"{where}.min_version must be a string.")
        detect = requires_app.get("detect")
        if detect is not None:
            if not isinstance(detect, dict) or not detect:
                raise ValueError(
                    f"{where}.detect must be a non-empty object with one of "
                    f"{sorted(_NATIVE_RECIPE_DETECT_KEYS)}."
                )
            unknown = set(detect.keys()) - _NATIVE_RECIPE_DETECT_KEYS
            if unknown:
                raise ValueError(
                    f"{where}.detect has unknown key(s) {sorted(unknown)}; "
                    f"expected only {sorted(_NATIVE_RECIPE_DETECT_KEYS)}."
                )

    for os_name, recipe in known.items():
        if not isinstance(recipe, dict) or not str(recipe.get("command") or "").strip():
            raise ValueError(
                f"native.platforms.{os_name}.command is required and must be "
                "a non-empty string."
            )
        args_template = recipe.get("args_template")
        if args_template is not None and not isinstance(args_template, list):
            raise ValueError(
                f"native.platforms.{os_name}.args_template must be a list."
            )
        env = recipe.get("env")
        if env is not None and not isinstance(env, dict):
            raise ValueError(f"native.platforms.{os_name}.env must be an object.")
        # GP.16c — per-OS app requirement; an OS entry WITHOUT the key
        # falls back to the node-level requires_app at resolution time.
        if "requires_app" in recipe:
            _check_requires_app(recipe["requires_app"],
                                f"native.platforms.{os_name}.requires_app")

    requires_app = custom_metadata.get("requires_app")
    if requires_app is not None:
        _check_requires_app(requires_app)

    return custom_metadata

class NodeDefinitionBase(BaseModel):
    name: str = Field(..., description="Unique name for the node definition (e.g., 'resize-image', 'convert-pdf-to-text').")
    description: Optional[str] = Field(None, description="A brief description of what this node does.")
    type: str = Field(..., description="What type of node this node is. (e.g. Texture for 'ambient occlusion')")
    executable_path: str = Field(..., description="Path to the executable (e.g., Docker image tag, script path) for this node.")
    # Optional. metadata files no longer carry process_type — scheduler
    # derives queue_name from NodeDefinition.gpu_variants at enqueue
    # time via utils.queue_selector.select_queue_for_node. The wizard's
    # /build-and-register-local may still write this as a human-
    # readable label (e.g. "CUDA" for single-variant nodes).
    process_type: Optional[str] = Field(
        None,
        description=(
            "Legacy hardware label (e.g. 'CPU', 'CUDA'). Informational "
            "only — routing is derived from gpu_variants."
        ),
    )
    input_schema: Dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema defining the expected input data structure for this node."
    )
    output_schema: Dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema defining the output data structure produced by this node."
    )
    configuration_schema: Dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema defining the configurable parameters for this node."
    )
    custom_metadata: Optional[Dict[str, Any]] = None
    cost_in_credits: int = Field(0, description="The computational cost associated with running this node.")
    batch_mode: str = Field("batch", description="Execution mode: 'batch' (process files independently) or 'grouped' (process all files together)")
    node_kind: Optional[str] = None
    accepted_formats: Optional[List[str]] = Field(None, description="File extensions this node accepts (e.g., ['jpg', 'png', 'tif']). Null means accept all.")
    gpu_variants: Optional[Dict[str, str]] = Field(None, description="GPU backend → image tag mapping (e.g., {'cuda': 'img:latest', 'rocm': 'img:latest-rocm'})")
    system_only: bool = Field(False, description="DERIVED alias of node_role (kept in sync). Prefer node_role. True == node_role 'system'.")
    node_role: Optional[str] = Field(None, description="Authoritative classification: 'user' (in the palette + wizard) or 'system' (hidden from users; orchestrator-only, e.g. auto-converters/benchmarks). Defaults to 'system' (conservative) when omitted on create.")
    container_timeout: int = Field(1800, description="Max container runtime in seconds before the task is killed.")
    allowed_egress: Optional[List[Dict[str, Any]]] = Field(
        None,
        description=(
            "Per-node outbound network allowlist — list of "
            "{host, ports, protocol} entries. Empty list or None means "
            "default-deny: the node can only reach internal MinIO + API "
            "endpoints. Non-empty list grants outbound to each listed "
            "host:port. Enforced by the worker's runtime sandbox — a "
            "compromised node cannot phone home to arbitrary servers."
        ),
    )
    use_sandbox_runtime: bool = Field(
        False,
        description=(
            "Opt-in gVisor (runsc) OCI runtime for this node's containers. "
            "Extra VM-grade isolation from the host kernel at ~2-3× perf "
            "cost. Available in every edition. Silently falls back to the "
            "default runtime on workers where gVisor isn't installed."
        ),
    )
    # FX.91 p2a (owner Q77=A) — the manifest's declared external hostnames.
    # Read-only here: the WRITE path validates through
    # services/node_egress_domains.normalise_egress_domains (see the strict
    # validator on NodeDefinitionCreate); the READ path only coerces a
    # hand-edited/garbage row to None so one bad row can never 500 the
    # palette list. p2b renders this in the palette; p2c gates it.
    egress_domains: Optional[List[str]] = Field(
        None,
        description=(
            "External hostnames this node declares it needs, e.g. "
            "['huggingface.co', '*.cdn.jsdelivr.net']. Plain hostnames only "
            "— no scheme, port, path or IP literals; a leading '*.' label is "
            "the only wildcard (any subdomain, not the apex). None/empty = "
            "the node declares no external host = default-deny (the worker "
            "puts its container on the internal-only, zero-outbound network). "
            "Stored canonically: deduped + sorted. Distinct from "
            "allowed_egress, the dispatch-time {host,ports,protocol} shape."
        ),
    )
    image_size_bytes: Optional[int] = Field(
        None,
        description=(
            "Size of the pushed container image in bytes. Populated by "
            "the imagebuilder after `docker push` succeeds (via `docker "
            "inspect`). Counts toward the tenant's storage_used_bytes "
            "quota. Defaults to 0 for rows built before this was wired."
        ),
    )
    # FX.73 p4c (owner decision Q56=A) — CE-inclusion source of truth per node.
    # Stored column node_definitions.ce_included, stamped TRUE on CREATE by
    # services/node_scanner.py for nodes in the CE node-seed keep-list
    # (open-core/node-seed-keeplist.yaml). The palette API returns it so the
    # WorkflowBuilder can badge "CE". None = not stamped / not CE-included
    # (no badge, never a guessed one).
    ce_included: Optional[bool] = Field(
        None,
        description=(
            "True when the node is included in the CE (open-source) edition "
            "— stamped from the CE node-seed keep-list at seed time. None for "
            "nodes the CE edition does not ship."
        ),
    )
    metadata_file_source: Optional[str] = Field(
        None,
        description=(
            "The rendered <Name>_<Type>_PROCESS_Node.py METADATA BLOCK "
            "source — the 4th Albedo artifact. Produced server-side by "
            "utils.metadata_template.render from the wizard's metadata dict, "
            "so the wizard's live preview matches what ships byte-for-"
            "byte. System nodes and image-mode nodes leave this NULL."
        ),
    )

def _strict_node_allowed_egress(v):
    """Shared strict validation for ``allowed_egress`` on write-path models.

    Used via ``@field_validator`` on both ``NodeDefinitionCreate`` and
    ``ExternalNodeAuthoringRequest``.
    """
    if v is None or v == []:
        return v
    from services.egress_allowlist import (
        compile_allowlist, EgressAllowlistError,
        classify_breadth,
    )

    # Breadth gate FIRST (catches public-suffix wildcards that may also
    # cause compile to fail).  compile_allowlist is called next so
    # structural problems (bad ports, bad hostnames) surface.
    reasons = classify_breadth(v)
    if reasons:
        raise ValueError(
            f"broad egress must be declared on a reviewed stack: {'; '.join(reasons)}"
        )

    try:
        compile_allowlist(v)
    except EgressAllowlistError as e:
        raise ValueError(str(e)) from None

    return v


class NodeDefinitionCreate(NodeDefinitionBase):
    # This class inherits process_type from NodeDefinitionBase, so it's ready for creation
    tenant_id: Optional[int] = Field(None, description="Tenant ID for tenant-specific nodes. NULL for system nodes visible to all tenants.")
    build_spec_id: Optional[int] = Field(None, description="Originating BuildSpec id when this create comes from a build-task register callback. Lets the credit charge bill the build's triggering user instead of the service account.")
    # sz-tcp.5c — wizard-chosen tier prices (sz-tcp.5b persisted cost_tiers +
    # cost_basis onto build_specs.spec_json; the build-task register stage now
    # forwards them). WRITE path: an explicit map lands verbatim on
    # node_definitions.cost_credits_by_tier with cost_basis='manual' instead of
    # utils/node_cost re-deriving 0.5x/1x/2.5x ratios. models.py CHECK
    # ck_node_def_cost_in_credits_matches_medium requires cost_in_credits ==
    # map['medium'] when the map is non-empty, so the register stage must send
    # cost_in_credits = medium (the wizard endpoint already forces that).
    # None == "not sent": SQLAlchemy omits an explicit None for these
    # server_default columns on insert, so a legacy create writes exactly
    # what it used to; on UPDATE the service pops them so they are never
    # setattr'd to NULL into the NOT-NULL columns.
    cost_credits_by_tier: Optional[Dict[str, Any]] = None
    cost_basis: Optional[str] = None

    # GP.4 / D-P1 — validated only on the write path (create), never on the
    # NodeDefinition read/response model: a pre-existing malformed row must
    # keep degrading gracefully (see `_coerce_bad_dict_to_none` above), not
    # 500 the whole palette list.
    @field_validator("custom_metadata")
    @classmethod
    def _validate_native_recipe(cls, v):
        return _validate_native_recipe_metadata(v)

    # C3: egress allowlist strict validation (WRITE path only).
    # compile + breadth gate so broad egress can only land on a reviewed stack.
    @field_validator("allowed_egress")
    @classmethod
    def _validate_allowed_egress(cls, v):
        return _strict_node_allowed_egress(v)

    # FX.91 p2a (owner Q77=A) — strict manifest grammar, WRITE path only
    # (same GP.4/D-P1 rule as custom_metadata above: a pre-existing bad row
    # must keep degrading on the read models, not 500 the palette). The
    # manifest's declared hostnames are validated + normalised here so the
    # stored value is always the canonical deduped/sorted list.
    @field_validator("egress_domains")
    @classmethod
    def _validate_egress_domains(cls, v):
        if v is None:
            return None
        from services.node_egress_domains import (
            EgressDomainError, normalise_egress_domains,
        )
        try:
            return normalise_egress_domains(v)
        except EgressDomainError as e:
            raise ValueError(str(e)) from None

class NodeDefinitionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    executable_path: Optional[str] = None
    process_type: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None
    configuration_schema: Optional[Dict[str, Any]] = None
    custom_metadata: Optional[Dict[str, Any]] = None
    cost_in_credits: Optional[int] = None
    # sz-tcp.5c — see NodeDefinitionBase. PATCHable so an explicit re-register
    # (wizard tier prices) can land the map + basis verbatim; the GB.TIER.0.4
    # re-scan guard still protects an operator-set price when NO tiers are sent.
    cost_credits_by_tier: Optional[Dict[str, Any]] = None
    cost_basis: Optional[str] = None
    batch_mode: Optional[str] = None
    accepted_formats: Optional[List[str]] = None
    gpu_variants: Optional[Dict[str, str]] = None
    system_only: Optional[bool] = None
    node_role: Optional[str] = None
    container_timeout: Optional[int] = None
    # FX.103 (owner Q89=A) — deprecation lifecycle. Sent together they drive a
    # validated transition (services/node_deprecation.validate_deprecation:
    # deprecated/retired requires deprecated_at; retire_after >= deprecated_at
    # + deprecation.min_runnable_months). Sent alone (e.g. status='deprecated'
    # with no dates) deprecated_at defaults to now and retire_after stays
    # unset = runnable for pinned workflows until retired.
    status: Optional[str] = None
    deprecated_at: Optional[datetime] = None
    retire_after: Optional[datetime] = None
    # FX.91 p2a — the manifest's declared hostnames, PATCHable so a
    # re-register/re-scan updates them alongside the rest of the manifest.
    # Same strict grammar as the create path (services/node_egress_domains).
    egress_domains: Optional[List[str]] = None

    @field_validator("custom_metadata")
    @classmethod
    def _validate_native_recipe(cls, v):
        return _validate_native_recipe_metadata(v)

    @field_validator("egress_domains")
    @classmethod
    def _validate_egress_domains(cls, v):
        if v is None:
            return None
        from services.node_egress_domains import (
            EgressDomainError, normalise_egress_domains,
        )
        try:
            return normalise_egress_domains(v)
        except EgressDomainError as e:
            raise ValueError(str(e)) from None


class NodeDefinition(NodeDefinitionBase):
    id: int
    tenant_id: Optional[int] = None
    # Overridden optional for the READ/response model: human-in-the-loop nodes
    # (e.g. "Human Approval") have no container image or config schema, so these
    # are NULL in the DB. The base declares them required for CREATE, but the
    # response model must tolerate those null system rows — otherwise one such
    # node raises ResponseValidationError and 500s the ENTIRE node-list, which
    # renders the Workflow Builder palette empty.
    executable_path: Optional[str] = None
    configuration_schema: Optional[Dict[str, Any]] = None
    scan_status: Optional[str] = None
    scan_result: Optional[Dict[str, Any]] = None
    rejection_reason: Optional[str] = None
    calibration_status: Optional[str] = None
    calibration_progress: Optional[Dict[str, Any]] = None
    # node-version-pinning — the node's current (newest) NodeVersion display
    # number, so the builder can show a "vN" badge and detect "update available"
    # by comparing against a workflow node's pinned version. Populated by the
    # list endpoint; None when the node has no recorded version (legacy/no digest).
    current_version: Optional[int] = None
    # The NodeVersion id of that current build — what the builder writes into a
    # node's pinnedNodeVersionId when the user drops it or clicks "update to vN".
    current_node_version_id: Optional[int] = None
    # FX.103 (owner Q89=A) — deprecation/retirement lifecycle, returned by the
    # palette so the UI can badge it (badge itself is part 2). 'active' |
    # 'deprecated' | 'retired'; defaults to 'active' so pre-FX.103 rows
    # (NULL column on an un-migrated DB) serialize as active, not blank.
    status: Optional[str] = "active"
    deprecated_at: Optional[datetime] = None
    retire_after: Optional[datetime] = None
    # FX.103 part 2 — the names the WorkflowBuilder palette reads for the
    # deprecation badge: lifecycle_state (='status') + retire_at (='retire_after').
    # Populated as transient attributes by the list endpoint; part-1 names
    # stay so pre-part-2 clients keep working.
    lifecycle_state: Optional[str] = "active"
    retire_at: Optional[datetime] = None
    # FX.91 p2b [U] — per-tenant egress-approval state for the palette badge,
    # derived (transient attribute) by the list/detail endpoints via
    # services/node_egress_approval.palette_egress_states — ONE batched query
    # per page, never per node. Shape:
    #   {'state': 'approved'|'required'|'stale'|'not_required',
    #    'declared': [...], 'approved': [...],
    #    'approval_id', 'approved_at', 'approved_by_user_id', 'revoked_at',
    #    'gov_blocked': bool, 'gov_denied': [...]}
    # None when the enrichment could not run (the UI then falls back to the
    # node's own `egress_domains` and shows no approval claim). NOT stored.
    egress_approval: Optional[Dict[str, Any]] = None
    # node-ownership-purchases — one-time price + display tier (from the model),
    # and per-tenant ownership state populated by the store/list endpoint.
    price_cents: Optional[int] = None
    price_tier: Optional[str] = None
    owned: Optional[bool] = None
    ownership_source: Optional[str] = None  # 'plan_grant' | 'purchase' | 'admin_grant'
    # sz-tcp.5c — tiered pricing read-back so the palette/UI can show per-tier
    # cost. Maps onto the deferred() columns node_definitions.cost_credits_by_tier
    # + cost_basis (migrations/20260513_tiered_credit_pricing); NULL/{} on rows
    # predating tiered pricing. Joined the bad-jsonb coercion group below so one
    # corrupt row degrades instead of 500-ing the whole palette.
    cost_credits_by_tier: Optional[Dict[str, Any]] = None
    cost_basis: Optional[str] = None

    # GO.9 — honest per-accelerator labels. `{variant: {status, perf_ratio}}`,
    # e.g. {"cuda": {"status": "verified", "perf_ratio": 1.0}, "rocm":
    # {"status": "unverified", "perf_ratio": null}}. status is one of
    # verified/unverified/incompatible/not_recommended. Derived (transient
    # attribute set by the list/detail endpoints via
    # utils.queue_selector.compute_variant_status) from existing columns —
    # NOT stored. None when the node has no variants to report.
    variant_status: Optional[Dict[str, Dict[str, Any]]] = None

    # FX.73 p4b — licence badge for the palette. Derived (transient attribute
    # set by the list endpoint via services/licence_detect.py::
    # licence_badges_for_nodes) from the node's newest live LicenceRecord —
    # `{"spdx": <spdx_id or licence_name>, "commercial_use": True|False|None}`
    # (True=allowed, False=non_commercial/restricted, None=unknown). NOT
    # stored. None when the node has no licence record at all.
    licence: Optional[Dict[str, Any]] = None

    # Defense-in-depth against corrupt jsonb rows blanking the whole palette.
    # Legacy/bad-seed rows can hold a JSON *string* sentinel (e.g. "NONE") in a
    # jsonb column typed as an object/array. `Optional[Dict]` tolerates NULL but
    # NOT a bare string, so a single such row raises ResponseValidationError and
    # 500s the ENTIRE List[NodeDefinition] response — rendering the Workflow
    # Builder palette empty for every user in that tenant. Coerce any non
    # dict/list value to None (optional fields) or {} (required schema fields)
    # so one bad row degrades to a slightly-incomplete node instead of nuking
    # the palette. (The underlying data is still repaired by the
    # 20260907_repair_node_config_schema_and_seed_qa_tenants migration.)
    @field_validator(
        "configuration_schema", "custom_metadata", "scan_result",
        "calibration_progress", "gpu_variants", "cost_credits_by_tier",
        mode="before",
    )
    @classmethod
    def _coerce_bad_dict_to_none(cls, v):
        if v is None or isinstance(v, dict):
            return v
        return None

    # FX.91 p2a: egress_domains joins the read-path coercion group so a
    # garbage row degrades to None (= default-deny), never 500s the palette.
    @field_validator(
        "accepted_formats", "allowed_egress", "egress_domains", mode="before"
    )
    @classmethod
    def _coerce_bad_list_to_none(cls, v):
        if v is None or isinstance(v, list):
            return v
        return None


    @field_validator("input_schema", "output_schema", mode="before")
    @classmethod
    def _coerce_bad_required_dict_to_empty(cls, v):
        if isinstance(v, dict):
            return v
        return {}

    model_config = ConfigDict(from_attributes=True)

class NodeCheckoutResponse(BaseModel):
    """node-ownership-purchases — result of acquiring a marketplace node.

    status:
      'granted'  — free node or already owned → entitlement present now.
      'checkout' — priced + unowned → redirect the user to `checkout_url`;
                   ownership is granted by the Stripe payment-success webhook.
    """
    status: str
    checkout_url: Optional[str] = None
    entitlement_id: Optional[int] = None


class NodePriceUpdate(BaseModel):
    """Admin — set/clear a maintained node's one-time ownership price.

    price_cents None or 0 clears the price (node becomes free).
    """
    price_cents: Optional[int] = None
    price_tier: Optional[str] = None


class NodePackageCreate(BaseModel):
    """Admin — create an official node package (bundle)."""
    name: str
    description: Optional[str] = None
    slug: Optional[str] = None
    price_cents: Optional[int] = None
    price_tier: Optional[str] = None
    node_definition_ids: List[int] = []


class NodePackageItemAdd(BaseModel):
    node_definition_id: int


class NodePackageResponse(BaseModel):
    """A node package for the store: its nodes + this tenant's ownership state."""
    id: int
    name: str
    description: Optional[str] = None
    price_cents: Optional[int] = None
    price_tier: Optional[str] = None
    node_definition_ids: List[int] = []
    owned: bool = False
    visibility: str = "public"
    allowed_tenant_ids: List[int] = []


# GG.6c (gg-commerce) — private listings: shared visibility/sharing update
# body for both NodePackage (bundle) and WorkflowListing publisher
# endpoints (routers/listing_visibility.py).
class ListingVisibilityUpdate(BaseModel):
    visibility: Optional[Literal["public", "private", "unlisted"]] = None
    allowed_tenant_ids: Optional[List[int]] = None
    allowed_emails: Optional[List[str]] = None


class ListingVisibilityResponse(BaseModel):
    id: int
    kind: Literal["node_package", "workflow_listing"]
    visibility: str
    allowed_tenant_ids: List[int]
    unresolved_emails: List[str] = []


# GG.2 (gg-commerce) — the unified store browse feed: nodes/stacks/workflows/
# bundles in one flat, kind-tagged list. See services/catalog_service.py.
class CatalogItem(BaseModel):
    kind: Literal["node", "stack", "workflow", "bundle"]
    id: int
    name: str
    description: Optional[str] = None
    price_cents: Optional[int] = None
    price_tier: Optional[str] = None
    owned: bool = False
    included_in: Optional[str] = None
    status: Optional[str] = None
    visibility: Optional[str] = None
    # GG.UI.7b — the pinned StackPackage's version (stack rows only;
    # StackPackage.version is String(64)). Additive: absent on the other
    # kinds' rows, defaults None.
    version: Optional[str] = None
    # GG.UI.7b — the seller-view "currently in the store" flag. True when
    # the row passes that kind's listing gate (stack: is_listed;
    # node/bundle: is_in_marketplace; workflow: status=='published').
    # None for rows where the distinction doesn't apply (plain browse).
    is_listed: Optional[bool] = None
    # GG.UI.7b — the publishing tenant, when the listing row carries one
    # (StackListing / WorkflowListing). None for node/bundle rows, which
    # have no publisher_tenant_id column.
    publisher_tenant_id: Optional[int] = None
    # GSEC FX.95 p5a — the listing was DELISTED by staff (a customer report
    # was actioned). Additive with a False default so every existing client
    # is unchanged. Delisted rows are hidden from other tenants' catalog
    # (their `status` is 'unlisted'), so this flag only ever shows for the
    # publisher's own view (GET /v1/catalog/mine) and for platform admins
    # browsing with include_unlisted. Node/bundle rows have no review state
    # and are always False.
    delisted: bool = False
    # GSEC FX.95 p4a (owner decision Q81=A) — read-only Verified-badge flag:
    # True when the listing's publisher tenant has a 'verified' publisher
    # verification record (services/publisher_verification.py). Additive with a
    # False default, so every existing caller and client is unchanged; p4b
    # renders the badge from this. Node/bundle rows have no publisher tenant,
    # so they are always False.
    publisher_verified: bool = False
    # FORTRESS 5.11 slice 1 — read-only certification badge source: True for
    # workflow rows with an active, unexpired, unrevoked
    # `WorkflowCertification` (services/catalog_service.py
    # `_active_certified_workflow_ids`). Additive with a False default, so
    # every existing caller/client is unchanged. Always False for other
    # kinds (node/stack/bundle rows have no `source_workflow_id`).
    is_certified: bool = False
    # 13.5f — the tenant secrets the pinned stack package declares
    # (stack detail rows only): `{name, kind, services, required,
    # description}` — names/kinds/descriptions, NEVER secret values.
    # None on the other kinds' rows.
    required_secrets: Optional[List[Dict[str, Any]]] = None


# --- External node-authoring API (open-core wizard replacement) --------------
# A deliberately NARROWED public contract for "register a node I built myself".
# It exposes only author-appropriate manifest fields; identity/tenant/role and
# every privileged attribute are derived server-side and are intentionally NOT
# present here, so a caller cannot mint a system/cross-tenant/published node.
class ExternalNodeAuthoringRequest(BaseModel):
    """Register (source='image') or build-and-register (source='dockerfile') a
    self-built node. No server-side LLM/codegen is involved on either path."""
    source: Literal["image", "dockerfile"] = Field(
        ..., description="'image' = register a pullable pre-built image; "
                         "'dockerfile' = build a self-contained Dockerfile then register."
    )
    # source='image'
    image: Optional[str] = Field(
        None, description="Pullable image reference (source='image'). Becomes the node's executable_path."
    )
    # source='dockerfile'
    dockerfile: Optional[str] = Field(
        None, description="Self-contained Dockerfile (source='dockerfile'): FROM an allowlisted base, "
                         "run as non-root, define its own ENTRYPOINT reading /data/input and writing /data/output."
    )
    container_env: Optional[Dict[str, str]] = Field(
        None, description="Extra build-time/runtime env vars for the built image (source='dockerfile')."
    )

    # --- Author-editable manifest subset (maps 1:1 onto NodeDefinitionBase) ---
    name: str = Field(..., description="Node display name.")
    type: str = Field(..., description="Node type / category.")
    description: Optional[str] = None
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    # NOTE: internal model field is `configuration_schema`.
    configuration_schema: Dict[str, Any] = Field(default_factory=dict)
    accepted_formats: Optional[List[str]] = None
    cost_in_credits: int = 0
    batch_mode: str = "batch"
    container_timeout: int = 1800
    allowed_egress: Optional[List[Dict[str, Any]]] = Field(
        None, description="Per-node outbound egress allowlist ({host,ports,protocol}); None/empty = default-deny."
    )
    use_sandbox_runtime: bool = False
    gpu_variants: Optional[Dict[str, str]] = Field(
        None, description="Optional backend->image-tag map for per-hardware variants."
    )
    process_type: Optional[str] = Field(
        None, description="Informational hardware label (CPU/GPU); defaults to CPU on the build path."
    )
    # FX.91 p2a (owner Q77=A) — author-declared external hostnames (the
    # manifest half of the egress default-deny contract). Plain hostnames,
    # leading '*.' wildcard only; validated by services/node_egress_domains
    # so the stored list is canonical. None/empty = default-deny.
    egress_domains: Optional[List[str]] = Field(
        None,
        description=(
            "External hostnames this node needs, e.g. "
            "['huggingface.co', '*.cdn.jsdelivr.net'] — plain hostnames only "
            "(no scheme/port/path/IP), '*.' only as the first label. "
            "None/empty = default-deny (internal-only network)."
        ),
    )

    @field_validator("egress_domains")
    @classmethod
    def _validate_egress_domains(cls, v):
        if v is None:
            return None
        from services.node_egress_domains import (
            EgressDomainError, normalise_egress_domains,
        )
        try:
            return normalise_egress_domains(v)
        except EgressDomainError as e:
            raise ValueError(str(e)) from None

    # C3: same strict egress allowlist validation as NodeDefinitionCreate.
    @field_validator("allowed_egress")
    @classmethod
    def _validate_allowed_egress(cls, v):
        return _strict_node_allowed_egress(v)

    @field_validator("batch_mode")
    @classmethod
    def _validate_batch_mode(cls, v: str) -> str:
        if v not in ("batch", "grouped"):
            raise ValueError("batch_mode must be 'batch' or 'grouped'")
        return v

    @model_validator(mode="after")
    def _validate_source_payload(self) -> "ExternalNodeAuthoringRequest":
        if self.source == "image":
            if not (self.image and self.image.strip()):
                raise ValueError("source='image' requires a non-empty 'image' reference")
        elif self.source == "dockerfile":
            if not (self.dockerfile and self.dockerfile.strip()):
                raise ValueError("source='dockerfile' requires a non-empty 'dockerfile'")
        return self


class ExternalNodeCreated(BaseModel):
    """201 response for source='image' (node registered immediately)."""
    id: int
    name: str
    node_role: Optional[str] = None
    scan_status: Optional[str] = None


class ExternalNodeBuildAccepted(BaseModel):
    """202 response for source='dockerfile' (build enqueued; poll build status)."""
    task_id: int
    step: str = "external-node-authoring"
    status: str = "pending"


class WorkflowExecutionRequest(BaseModel):
    nodes: List[Dict[str, Any]]
    edges: List[Dict[str, Any]]

class FileCreate(BaseModel):
    name: str
    size_bytes: int
    mime_type: str
    storage_path: str
    cog_id: Optional[int] = None  # Optional folder assignment

class File(FileCreate):
    id: str
    tenant_id: int
    user_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class WorkflowRunFileBase(BaseModel):
    title: str
    status: Optional[str] = models.TaskStatus.QUEUED.value

class WorkflowRunFileCreate(WorkflowRunFileBase):
    pass

class WorkflowRunFileUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None

class WorkflowRunFile(WorkflowRunFileBase):
    id: int
    workflow_run_id: int
    file_id: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class NodeRunStatusUpdate(BaseModel):
    status: TaskStatus
    inputs: Optional[Dict[str, Any]] = None
    outputs: Optional[List[Dict[str, Any]]] = None
    error_message: Optional[str] = None

class EmailConfigBase(BaseModel):
    provider: Optional[str] = 'brevo'  # 'brevo', 'mailgun', 'custom' (SMTP)
    sender_name: Optional[str] = None
    sender_email: Optional[EmailStr] = None

    # Legacy field
    email_from: Optional[EmailStr] = None

class EmailConfigCreate(EmailConfigBase):
    # Custom SMTP fields (only used for 'custom' provider)
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None

    # Legacy field
    sendgrid_api_key: Optional[str] = None

class EmailConfigUpdate(EmailConfigBase):
    # Custom SMTP fields (only used for 'custom' provider)
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None

    # Legacy field
    sendgrid_api_key: Optional[str] = None

class EmailConfig(EmailConfigBase):
    # This schema is used for API responses. We don't want to expose secrets,
    # but we do want to let the frontend know if keys have been set.
    smtp_password_set: bool = False

    # Custom SMTP fields (safe to return)
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None

    # Legacy field
    sendgrid_api_key_set: bool = False

    model_config = ConfigDict(from_attributes=True)

class SMTPSettingsBase(BaseModel):
    smtp_server: str
    smtp_port: int
    smtp_username: str


class SMTPSettingsCreate(SMTPSettingsBase):
    smtp_password: str


class SMTPSettingsUpdate(SMTPSettingsBase):
    smtp_password: Optional[str] = None


class SMTPSettings(SMTPSettingsBase):
    id: int
    user_id: int

    model_config = ConfigDict(from_attributes=True)


# --- Permission Audit Log Schemas ---
class PermissionAuditLogBase(BaseModel):
    action: str
    resource_type: str
    resource_id: int
    permission_level: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class PermissionAuditLog(PermissionAuditLogBase):
    id: int
    tenant_id: int
    timestamp: datetime
    actor_user_id: int
    target_user_id: Optional[int] = None
    permission_id: Optional[int] = None
    permission_request_id: Optional[int] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None

    # Optional nested user info for display
    actor_email: Optional[str] = None
    target_email: Optional[str] = None

    # Resource context for grouping
    resource_name: Optional[str] = None
    parent_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# --- Permission Template Schemas ---
class PermissionTemplateBase(BaseModel):
    name: str
    description: Optional[str] = None
    permission_level: str  # viewer, editor, owner

    # Capability overrides (None means use level defaults)
    can_upload: Optional[bool] = None
    can_create_subfolders: Optional[bool] = None
    can_move_files: Optional[bool] = None
    can_process: Optional[bool] = None
    can_share: Optional[bool] = None
    can_delete: Optional[bool] = None

    # Default expiration
    default_duration_days: Optional[int] = None


class PermissionTemplateCreate(PermissionTemplateBase):
    pass


class PermissionTemplateUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    permission_level: Optional[str] = None
    can_upload: Optional[bool] = None
    can_create_subfolders: Optional[bool] = None
    can_move_files: Optional[bool] = None
    can_process: Optional[bool] = None
    can_share: Optional[bool] = None
    can_delete: Optional[bool] = None
    default_duration_days: Optional[int] = None


class PermissionTemplate(PermissionTemplateBase):
    id: int
    tenant_id: int
    is_system_template: bool
    created_by_user_id: int
    created_at: datetime
    updated_at: datetime
    usage_count: int

    model_config = ConfigDict(from_attributes=True)


class ApplyTemplateRequest(BaseModel):
    """Request to apply a template to users/folders."""
    template_id: int
    cog_ids: List[int]  # Folders to apply to
    user_ids: Optional[List[int]] = None  # Users to grant
    emails: Optional[List[str]] = None  # External users to invite
    override_duration_days: Optional[int] = None  # Override template's default duration


# --- Tenant Schemas ---
class TenantCreate(BaseModel):
    """Schema for creating a new tenant (platform admin only)."""
    name: str
    credit_balance: Optional[int] = 10000  # Default initial credits

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        # LDF-3 §10 — single source of truth for tenant-name rules.
        from validators.identity import validate_company_name, CompanyNameValidationError
        try:
            return validate_company_name(v)
        except CompanyNameValidationError as e:
            raise ValueError(e.message) from None


class Tenant(BaseModel):
    """Tenant information for platform admin."""
    id: int
    name: str
    credit_balance: int
    storage_used_bytes: int
    created_at: datetime
    # Plan id of the tenant's active subscription (None if unsubscribed).
    # Lets the admin custom-plans UI render real "Assigned" status.
    current_plan_id: Optional[int] = None
    # GG.HQ.2 — per-tenant Harbor storage quota override (bytes).
    # None = plan-derived; -1 = unlimited; >0 = exact cap.
    harbor_storage_quota_override_bytes: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


# --- Billing Mode and Payment Method Schemas (Phase 1) ---
class BillingMode(BaseModel):
    """Billing mode configuration for a tenant."""
    billing_mode: str  # 'prepaid' or 'postpaid'
    has_payment_method: bool
    payment_method_last4: Optional[str] = None
    payment_method_brand: Optional[str] = None
    auto_charge_enabled: bool
    # True only when the tenant's plan supports pay-as-you-go overage
    # (paid metered plans). Free + unlimited tiers can't switch to
    # postpaid; the UI hides the "Switch to Pay-As-You-Go" button when
    # this is False (the API enforces the same rule server-side).
    postpaid_eligible: bool = False


class BillingModeChangeRequest(BaseModel):
    """Request to change billing mode."""
    billing_mode: str = Field(..., description="Billing mode (a mode the billing module defines)")


class BillingModeChangeResponse(BaseModel):
    """Response after changing billing mode."""
    billing_mode: str
    message: str
    requires_payment_method: bool


# --- Credit Usage Log Schemas (Phase 1) ---
class CreditUsageLog(BaseModel):
    """Credit usage log entry for postpaid billing."""
    id: int
    tenant_id: int
    workflow_run_id: Optional[int] = None
    node_kind: Optional[str] = None
    node_id: Optional[str] = None
    credits_used: int
    cost_usd_cents: int
    billing_period: str  # "2025-11"
    billed_in_invoice_id: Optional[int] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CurrentUsageResponse(BaseModel):
    """Current billing period usage summary."""
    billing_period: str
    period_start: datetime
    period_end: datetime
    total_credits_used: int
    estimated_cost_usd: float
    subscription_credits_included: int
    overage_credits: int
    overage_cost_usd: float
    breakdown: List[Dict[str, Any]] = []
    # persistent-stack-billing §5.3 — reservation burn (credits/hour = Σ running-
    # stack reservation rates) and projected runway (credits_remaining / burn;
    # null = unbounded, when burn is 0 or the tenant is postpaid). 0 / null while
    # metering is dark (rates 0), which is the honest pre-launch number.
    reservation_burn_credits_per_hour: float = 0.0
    reservation_runway_hours: Optional[float] = None


# --- Billing Invoice Schemas (Phase 1) ---
class BillingInvoice(BaseModel):
    """Billing invoice for postpaid or overage charges."""
    id: int
    tenant_id: int
    invoice_number: str
    billing_period: str
    subscription_fee_cents: int
    subscription_credits_included: int
    total_credits_used: int
    overage_credits: int
    usage_cost_cents: int
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    stripe_invoice_id: Optional[str] = None
    stripe_charge_id: Optional[str] = None
    status: str
    issued_at: datetime
    due_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InvoiceListResponse(BaseModel):
    """List of invoices for a tenant."""
    invoices: List[BillingInvoice]


class InvoiceDetailResponse(BillingInvoice):
    """Detailed invoice with usage line items."""
    stripe_invoice_url: Optional[str] = None


# --- Billing Settings Schemas (Phase 1) ---
class BillingSettings(BaseModel):
    """Global billing configuration (admin only)."""
    id: int
    credit_price_cents: int
    tier1_threshold: int
    tier1_price_cents: int
    tier2_threshold: int
    tier2_price_cents: int
    tier3_price_cents: int
    default_tax_rate: int
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BillingSettingsUpdate(BaseModel):
    """Update billing settings (admin only)."""
    credit_price_cents: Optional[int] = None
    tier1_threshold: Optional[int] = None
    tier1_price_cents: Optional[int] = None
    tier2_threshold: Optional[int] = None
    tier2_price_cents: Optional[int] = None
    tier3_price_cents: Optional[int] = None
    default_tax_rate: Optional[int] = None


# --- Billing Pricing Configuration Schemas (Admin Only) ---
class BillingPricingResponse(BaseModel):
    """Response for pricing configuration endpoints."""
    postpaid_credit_price_cents: int
    overage_credit_price_cents: int
    postpaid_price_display: str  # Formatted: "$0.10"
    overage_price_display: str  # Formatted: "$0.08"
    # payg 8.1 — per-meter postpaid prices echo back on the response
    postpaid_seat_price_cents_per_month: Optional[int] = None
    postpaid_storage_price_cents_per_gb_month: Optional[int] = None
    postpaid_concurrent_price_cents_per_month: Optional[int] = None
    last_updated: datetime


class BillingPricingUpdate(BaseModel):
    """Update pricing configuration (admin only)."""
    postpaid_credit_price_cents: Optional[int] = Field(
        None,
        ge=1,  # Must be at least 1 cent
        le=10000,  # Maximum $100.00 per credit
        description="Configured rate for this user class (whole units)"
    )
    overage_credit_price_cents: Optional[int] = Field(
        None,
        ge=1,  # Must be at least 1 cent
        le=10000,  # Maximum $100.00 per credit
        description="Rate for this tenant (cents). Must be between 1 and 10000 cents ($0.01 - $100.00)"
    )
    # payg 8.1 — per-meter postpaid rates (sz-payg.1 columns). Immediate-effect
    # (no grace period); null = leave unchanged.
    postpaid_seat_price_cents_per_month: Optional[int] = Field(
        None,
        ge=0,
        le=1000000,
        description="Configured rate for this user class (whole units)"
    )
    postpaid_storage_price_cents_per_gb_month: Optional[int] = Field(
        None,
        ge=0,
        le=1000000,
        description="Configured rate for this user class (whole units)"
    )
    postpaid_concurrent_price_cents_per_month: Optional[int] = Field(
        None,
        ge=0,
        le=1000000,
        description="Configured rate for this user class (whole units)"
    )
    change_reason: Optional[str] = Field(
        None,
        description="Optional reason for the pricing change (for audit trail)"
    )
    effective_at: Optional[datetime] = Field(
        None,
        description="Optional future date when pricing should take effect (for scheduled changes)"
    )


class PricingAuditLogEntry(BaseModel):
    """Single pricing change audit log entry."""
    id: int
    changed_by_user_id: int
    changed_by_email: Optional[str] = None  # Enriched from join
    old_postpaid_price_cents: Optional[int] = None
    new_postpaid_price_cents: Optional[int] = None
    old_overage_price_cents: Optional[int] = None
    new_overage_price_cents: Optional[int] = None
    change_reason: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    changed_at: datetime
    effective_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class PricingAuditLogResponse(BaseModel):
    """Response for pricing audit log history."""
    audit_logs: List[PricingAuditLogEntry]
    total_count: int


class TenantPricingOverride(BaseModel):
    """Schema for creating/updating tenant-specific pricing overrides (admin only)."""
    postpaid_price_cents: Optional[int] = Field(
        None,
        ge=1,
        le=10000,
        description="Custom rate for this tenant (cents)"
    )
    overage_price_cents: Optional[int] = Field(
        None,
        ge=1,
        le=10000,
        description="Custom overage price for this tenant (cents)"
    )
    override_reason: str = Field(
        ...,
        description="Reason for the pricing override (required for audit trail)"
    )
    expires_at: Optional[datetime] = Field(
        None,
        description="Optional expiration date for temporary pricing overrides"
    )


# --- Notification Schemas ---
class NotificationResponse(BaseModel):
    """Single notification for a user."""
    id: int
    recipient_user_id: int
    message: str
    is_read: bool
    is_archived: bool = False
    notification_type: Optional[str] = None
    link_to_cog_id: Optional[int] = None
    link_url: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NotificationListResponse(BaseModel):
    """List of notifications with pagination info."""
    notifications: List[NotificationResponse]
    total_count: int


# --- Billing Dashboard Schemas (Phase 2) ---
class UsageHistoryPeriod(BaseModel):
    """Usage data for a single billing period."""
    period: str  # "2025-11"
    period_display: str  # "November 2025"
    credits_used: int
    cost_usd: float
    invoice_id: Optional[int] = None
    invoice_status: Optional[str] = None


class UsageHistoryResponse(BaseModel):
    """Usage history for analytics charts."""
    history: List[UsageHistoryPeriod]


# --- Hardware Settings Schemas ---
class PlatformHardwareSettingsResponse(BaseModel):
    """Platform hardware tag configuration."""
    hardware_tags: List[str]
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

PlatformGPUSettingsResponse = PlatformHardwareSettingsResponse


class PlatformHardwareSettingsUpdate(BaseModel):
    """Update hardware tags. Any string is valid (e.g., cuda, rocm, tensorrt, groq).

    ``cuda`` is locked on: a non-empty list must include it (an explicit list
    that omits ``cuda`` is rejected). ``[]`` is allowed and means "auto — the
    whole enrolled fleet".
    """
    hardware_tags: List[str] = Field(..., description="Enabled hardware tags (e.g., ['cuda', 'rocm', 'tensorrt']). A non-empty list must include 'cuda' (locked on).")

PlatformGPUSettingsUpdate = PlatformHardwareSettingsUpdate


class UsageBreakdownItem(BaseModel):
    """Single item in usage breakdown."""
    node_name: str
    credits_used: int
    cost_usd: float
    run_count: int


class InvoiceDetail(BaseModel):
    """Detailed invoice information with usage breakdown."""
    id: int
    invoice_number: str
    billing_period: str
    status: str
    total_cents: int
    total_credits_used: int
    subscription_credits_included: int
    overage_credits: int
    usage_cost_cents: int
    subtotal_cents: int
    tax_cents: int
    issued_at: datetime
    due_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    stripe_charge_id: Optional[str] = None
    usage_breakdown: List[UsageBreakdownItem] = []


# --- Download Job Schemas ---
class DownloadJobCreate(BaseModel):
    """Schema for creating a download job."""
    cog_ids: List[int] = Field(..., description="List of cog IDs to download")


class DownloadJobResponse(BaseModel):
    """Schema for download job status response."""
    id: str
    status: DownloadJobStatus
    total_files: int
    processed_files: int
    current_file: Optional[str] = None
    progress_percent: int = 0  # Computed: (processed_files / total_files) * 100
    download_url: Optional[str] = None
    filename: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    expires_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class DownloadJobUpdateRequest(BaseModel):
    """Schema for updating download job progress (service endpoint)."""
    status: Optional[DownloadJobStatus] = None
    processed_files: Optional[int] = None
    current_file: Optional[str] = None
    total_files: Optional[int] = None
    download_url: Optional[str] = None
    filename: Optional[str] = None
    error_message: Optional[str] = None
    expires_at: Optional[datetime] = None


# --- Granular Permission System Schemas ---

class CapabilityBase(BaseModel):
    """Base schema for capability definitions."""
    resource_type: ResourceType
    name: str
    display_name: str
    description: Optional[str] = None
    category: str
    display_order: int = 0
    is_sensitive: bool = False
    requires_capabilities: Optional[List[str]] = None


class CapabilityResponse(CapabilityBase):
    """Response schema for capability definitions."""
    id: int

    model_config = ConfigDict(from_attributes=True)


class CapabilityGrouped(BaseModel):
    """Capabilities grouped by resource type for UI display."""
    resource_type: ResourceType
    resource_type_display: str
    categories: Dict[str, List[CapabilityResponse]]


class ResourcePermissionCreate(BaseModel):
    """Schema for creating a resource permission."""
    capability_id: int
    resource_type: ResourceType
    resource_id: Optional[int] = None  # NULL = wildcard (all resources of this type)

    # Grant to one of these
    user_id: Optional[int] = None
    team_id: Optional[int] = None
    role_id: Optional[int] = None

    # Optional constraints
    conditions: Optional[Dict[str, Any]] = None  # JSON conditions for the permission
    expires_at: Optional[datetime] = None


class ResourcePermissionUpdate(BaseModel):
    """Schema for updating a resource permission."""
    conditions: Optional[Dict[str, Any]] = None
    expires_at: Optional[datetime] = None


class ResourcePermissionResponse(BaseModel):
    """Response schema for resource permissions."""
    id: int
    capability_id: int
    capability_name: Optional[str] = None  # Populated from relationship
    capability_display_name: Optional[str] = None  # Populated from relationship
    resource_type: ResourceType
    resource_id: Optional[int] = None
    user_id: Optional[int] = None
    team_id: Optional[int] = None
    role_id: Optional[int] = None
    conditions: Optional[Dict[str, Any]] = None
    expires_at: Optional[datetime] = None
    granted_by_user_id: Optional[int] = None
    granted_at: Optional[datetime] = None
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


class ResourcePermissionBulkCreate(BaseModel):
    """Schema for bulk creating resource permissions."""
    capability_ids: List[int]
    resource_type: ResourceType
    resource_id: Optional[int] = None

    # Grant to one of these
    user_id: Optional[int] = None
    team_id: Optional[int] = None
    role_id: Optional[int] = None

    conditions: Optional[Dict[str, Any]] = None
    expires_at: Optional[datetime] = None


class RoleCapabilityAssignment(BaseModel):
    """Schema for assigning capabilities to a role."""
    capability_ids: List[int]


class CustomRoleWithCapabilities(BaseModel):
    """Role with its assigned capabilities."""
    id: int
    name: str
    description: Optional[str] = None
    is_system_role: bool = False
    capabilities: List[CapabilityResponse] = []

    model_config = ConfigDict(from_attributes=True)


class EffectivePermissions(BaseModel):
    """User's effective permissions on a specific resource."""
    resource_type: ResourceType
    resource_id: Optional[int] = None
    capabilities: List[str]  # List of capability names the user has
    sources: Dict[str, str]  # capability_name -> source (direct, role:RoleName, team:TeamName)


class PermissionCheckRequest(BaseModel):
    """Request to check if user has specific capability on resource."""
    capability_name: str
    resource_type: ResourceType
    resource_id: Optional[int] = None


class PermissionCheckResponse(BaseModel):
    """Response for permission check."""
    allowed: bool
    capability_name: str
    resource_type: ResourceType
    resource_id: Optional[int] = None
    source: Optional[str] = None  # How permission was granted (direct, role, team)
    conditions: Optional[Dict[str, Any]] = None  # Any conditions on the permission

# --- Legal Hold Schemas ---
class LegalHoldSet(BaseModel):
    """Request to set a legal hold on a cog."""
    reason: str = Field(..., min_length=1, max_length=1000, description="Reason for the legal hold")

class LegalHoldStatus(BaseModel):
    """Legal hold status for a cog."""
    cog_id: int
    legal_hold: bool
    reason: Optional[str] = None
    set_by_user_id: Optional[int] = None
    set_at: Optional[datetime] = None
    outputs_held: int = 0

    model_config = ConfigDict(from_attributes=True)


# --- Workflow Trigger Schemas ---

_TRIGGER_TYPE_PATTERN = (
    r'^(schedule|workflow_completion|webhook|file_event|log_pattern_alert|'
    r'queue_empty|interval|one_shot|node_completion|fan_in|'
    r'system_threshold|db_row_event|'
    r'api_call|email|chatops|sms|git|'
    r'manual|approval|form|composite|'
    r'message_bus|poll|mcp_tool_call)$'
)
# add-workflow-triggers §6.4/§6.5 — metric sources a system_threshold can watch.
_THRESHOLD_METRICS = {"queue_depth", "workers_online", "failed_runs"}
# WS4.Q17.11 — shape check only. The tenant-specific cross-tenant guard lives
# in services/workflow_triggers_service.create_trigger (compares against
# get_tenant_bucket_name, i.e. the tenant's EFFECTIVE bucket: BYO bucket when
# an ACTIVE connection exists, house default otherwise). A strict
# `^tenant-\d+-files$` here would reject BYO tenants' own valid buckets before
# the service check ever ran, so accept any S3-valid bucket name.
_BUCKET_PATTERN = r'^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$'
_ON_STATUS_VALUES = {"succeeded", "failed", "any"}
_FILE_EVENT_VALUES = {"created", "modified", "deleted", "any"}
_LOG_LEVEL_VALUES = {"WARNING", "ERROR", "CRITICAL", "any"}
_LOG_MATCH_FIELD_VALUES = {"message", "module", "any"}
_QUEUE_SCOPE_VALUES = {"tenant", "workflow"}


class WorkflowTriggerCreate(BaseModel):
    """Create a workflow trigger.

    `configuration` shape depends on `trigger_type`; see `_validate_configuration`.
    """
    workflow_id: int
    trigger_type: str = Field(default="schedule", pattern=_TRIGGER_TYPE_PATTERN)
    configuration: Dict[str, Any] = Field(..., description="Type-specific config")
    is_active: bool = True

    @model_validator(mode='after')
    def _validate_configuration(self) -> 'WorkflowTriggerCreate':
        import re as _re

        cfg = self.configuration or {}
        ttype = self.trigger_type

        if ttype == "schedule":
            cron = cfg.get("cron")
            if not isinstance(cron, str) or not cron.strip():
                raise ValueError(
                    "configuration.cron is required for schedule triggers"
                )
            parts = cron.strip().split()
            if len(parts) != 5:
                raise ValueError(
                    "configuration.cron must have 5 fields: minute hour day month weekday"
                )
            tz = cfg.get("timezone")
            if tz is not None and not isinstance(tz, str):
                raise ValueError("configuration.timezone must be a string")

        elif ttype == "workflow_completion":
            src = cfg.get("source_workflow_id")
            if not isinstance(src, int):
                raise ValueError(
                    "configuration.source_workflow_id (int) is required"
                )
            on_status = cfg.get("on_status")
            if on_status not in _ON_STATUS_VALUES:
                raise ValueError(
                    f"configuration.on_status must be one of {sorted(_ON_STATUS_VALUES)}"
                )
            fwd = cfg.get("forward_outputs", False)
            if not isinstance(fwd, bool):
                raise ValueError("configuration.forward_outputs must be a bool")
            cfg.setdefault("forward_outputs", False)

        elif ttype == "webhook":
            if "secret_hash" in cfg or "secret_token" in cfg:
                raise ValueError(
                    "configuration.secret_hash / secret_token are server-managed; "
                    "do not supply them on create"
                )
            methods = cfg.get("allowed_methods")
            if methods is None:
                cfg["allowed_methods"] = ["POST"]
            elif (
                not isinstance(methods, list)
                or not methods
                or any(not isinstance(m, str) for m in methods)
            ):
                raise ValueError(
                    "configuration.allowed_methods must be a non-empty list of strings"
                )
            fb = cfg.get("forward_body", False)
            if not isinstance(fb, bool):
                raise ValueError("configuration.forward_body must be a bool")

        elif ttype == "file_event":
            # add-workflow-triggers §6.1/6.2 — three delivery sources, one type:
            #   source=s3 (default):    cloud object storage; requires `bucket`.
            #   source=minio:           legacy alias (same as s3); requires `bucket`.
            #   source=fs:              on-prem worker filesystem; requires `path`.
            source = cfg.get("source", "s3")
            if source not in ("s3", "minio", "fs"):
                raise ValueError(
                    "configuration.source must be 's3' (legacy alias: 'minio') or 'fs'"
                )
            cfg.setdefault("source", source)
            if source in ("s3", "minio"):
                bucket = cfg.get("bucket")
                if not isinstance(bucket, str) or not _re.match(_BUCKET_PATTERN, bucket):
                    raise ValueError(
                        "configuration.bucket must match 'tenant-<id>-files'"
                    )
            else:  # fs
                path = cfg.get("path")
                if not isinstance(path, str) or not path.strip():
                    raise ValueError("configuration.path is required when source='fs'")
            for opt_key in ("prefix", "suffix", "glob"):
                v = cfg.get(opt_key)
                if v is not None and not isinstance(v, str):
                    raise ValueError(f"configuration.{opt_key} must be a string")
            event_type = cfg.get("event_type", "created")
            if event_type not in _FILE_EVENT_VALUES:
                raise ValueError(
                    f"configuration.event_type must be one of {sorted(_FILE_EVENT_VALUES)}"
                )
            cfg.setdefault("event_type", event_type)
            fop = cfg.get("forward_object_path", False)
            if not isinstance(fop, bool):
                raise ValueError("configuration.forward_object_path must be a bool")

        elif ttype == "log_pattern_alert":
            pattern = cfg.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                raise ValueError(
                    "configuration.pattern (regex string) is required"
                )
            try:
                _re.compile(pattern)
            except _re.error as exc:
                raise ValueError(
                    f"configuration.pattern is not a valid regex: {exc}"
                )
            log_level = cfg.get("log_level", "ERROR")
            if log_level not in _LOG_LEVEL_VALUES:
                raise ValueError(
                    f"configuration.log_level must be one of {sorted(_LOG_LEVEL_VALUES)}"
                )
            cfg.setdefault("log_level", log_level)
            match_field = cfg.get("match_field", "message")
            if match_field not in _LOG_MATCH_FIELD_VALUES:
                raise ValueError(
                    f"configuration.match_field must be one of {sorted(_LOG_MATCH_FIELD_VALUES)}"
                )
            cfg.setdefault("match_field", match_field)
            cooldown = cfg.get("cooldown_seconds", 60)
            if not isinstance(cooldown, int) or cooldown < 1:
                raise ValueError(
                    "configuration.cooldown_seconds must be an int >= 1"
                )
            cfg.setdefault("cooldown_seconds", cooldown)

        elif ttype == "queue_empty":
            scope = cfg.get("scope")
            if scope not in _QUEUE_SCOPE_VALUES:
                raise ValueError(
                    f"configuration.scope must be one of {sorted(_QUEUE_SCOPE_VALUES)}"
                )
            src = cfg.get("source_workflow_id")
            if scope == "workflow":
                if not isinstance(src, int):
                    raise ValueError(
                        "configuration.source_workflow_id (int) is required when scope='workflow'"
                    )
            else:
                if src is not None:
                    raise ValueError(
                        "configuration.source_workflow_id must be null when scope='tenant'"
                    )
            cooldown = cfg.get("cooldown_seconds", 300)
            if not isinstance(cooldown, int) or cooldown < 30:
                raise ValueError(
                    "configuration.cooldown_seconds must be an int >= 30"
                )
            cfg.setdefault("cooldown_seconds", cooldown)

        elif ttype == "interval":
            secs = cfg.get("interval_seconds")
            if not isinstance(secs, int) or secs < 10:
                raise ValueError(
                    "configuration.interval_seconds must be an int >= 10"
                )

        elif ttype == "one_shot":
            run_at = cfg.get("run_at")
            if not isinstance(run_at, str) or not run_at.strip():
                raise ValueError(
                    "configuration.run_at (ISO 8601 datetime string) is required"
                )
            from datetime import datetime as _dt
            try:
                _dt.fromisoformat(run_at.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("configuration.run_at must be a valid ISO 8601 datetime")

        elif ttype == "node_completion":
            if not isinstance(cfg.get("source_workflow_id"), int):
                raise ValueError("configuration.source_workflow_id (int) is required")
            if not isinstance(cfg.get("workflow_node_id"), int):
                raise ValueError("configuration.workflow_node_id (int) is required")
            on_status = cfg.get("on_status", "any")
            if on_status not in _ON_STATUS_VALUES:
                raise ValueError(
                    f"configuration.on_status must be one of {sorted(_ON_STATUS_VALUES)}")
            cfg.setdefault("on_status", on_status)

        elif ttype == "fan_in":
            sources = cfg.get("sources")
            if (not isinstance(sources, list) or len(sources) < 2
                    or any(not isinstance(s, int) for s in sources)):
                raise ValueError(
                    "configuration.sources must be a list of >=2 source workflow ids (int)")
            win = cfg.get("window_seconds", 3600)
            if not isinstance(win, int) or win < 1:
                raise ValueError("configuration.window_seconds must be an int >= 1")
            cfg.setdefault("window_seconds", win)
            on_status = cfg.get("on_status", "succeeded")
            if on_status not in _ON_STATUS_VALUES:
                raise ValueError(
                    f"configuration.on_status must be one of {sorted(_ON_STATUS_VALUES)}")
            cfg.setdefault("on_status", on_status)

        elif ttype == "system_threshold":
            if cfg.get("metric") not in _THRESHOLD_METRICS:
                raise ValueError(
                    f"configuration.metric must be one of {sorted(_THRESHOLD_METRICS)}")
            if cfg.get("op") not in ("gt", "lt", "gte", "lte"):
                raise ValueError("configuration.op must be one of gt/lt/gte/lte")
            if not isinstance(cfg.get("value"), (int, float)):
                raise ValueError("configuration.value (number) is required")
            rv = cfg.get("rearm_value", cfg["value"])
            if not isinstance(rv, (int, float)):
                raise ValueError("configuration.rearm_value must be a number")
            cfg.setdefault("rearm_value", rv)
            win = cfg.get("window_seconds", 300)
            if not isinstance(win, int) or win < 1:
                raise ValueError("configuration.window_seconds must be an int >= 1")
            cfg.setdefault("window_seconds", win)

        elif ttype == "db_row_event":
            if not isinstance(cfg.get("table"), str) or not cfg["table"].strip():
                raise ValueError("configuration.table (str) is required")
            if cfg.get("op", "insert") not in ("insert", "update", "delete", "any"):
                raise ValueError("configuration.op must be insert/update/delete/any")
            cfg.setdefault("op", cfg.get("op", "insert"))
            flt = cfg.get("filter")
            if flt is not None and not isinstance(flt, dict):
                raise ValueError("configuration.filter must be an object")

        elif ttype == "api_call":
            pass  # authenticated + tenant-scoped at the endpoint; no required config

        elif ttype == "email":
            if not isinstance(cfg.get("address"), str) or "@" not in cfg["address"]:
                raise ValueError("configuration.address (inbound email address) is required")

        elif ttype == "chatops":
            if cfg.get("provider") not in ("slack", "teams"):
                raise ValueError("configuration.provider must be 'slack' or 'teams'")
            if not isinstance(cfg.get("signing_secret"), str) or not cfg["signing_secret"]:
                raise ValueError("configuration.signing_secret is required")
            if cfg.get("command") is not None and not isinstance(cfg["command"], str):
                raise ValueError("configuration.command must be a string")

        elif ttype == "git":
            if cfg.get("provider") not in ("github", "gitlab"):
                raise ValueError("configuration.provider must be 'github' or 'gitlab'")
            if not isinstance(cfg.get("signing_secret"), str) or not cfg["signing_secret"]:
                raise ValueError("configuration.signing_secret is required")
            for opt in ("branch",):
                if cfg.get(opt) is not None and not isinstance(cfg[opt], str):
                    raise ValueError(f"configuration.{opt} must be a string")
            events = cfg.get("events")
            if events is not None and (not isinstance(events, list)
                                       or any(not isinstance(e, str) for e in events)):
                raise ValueError("configuration.events must be a list of strings")

        elif ttype == "manual":
            pass  # authenticated + tenant-scoped at the endpoint; no required config

        elif ttype == "approval":
            appr = cfg.get("approvers")
            if appr is not None and not isinstance(appr, list):
                raise ValueError("configuration.approvers must be a list")

        elif ttype == "form":
            if "secret_hash" in cfg or "secret_token" in cfg:
                raise ValueError("configuration secret is server-managed; do not supply it")
            fields = cfg.get("fields")
            if fields is not None and not isinstance(fields, list):
                raise ValueError("configuration.fields must be a list")

        elif ttype == "composite":
            if cfg.get("op") not in ("and", "or"):
                raise ValueError("configuration.op must be 'and' or 'or'")
            cons = cfg.get("constituents")
            if (not isinstance(cons, list) or len(cons) < 2
                    or any(not isinstance(c, int) for c in cons)):
                raise ValueError(
                    "configuration.constituents must be a list of >=2 trigger ids (int)")
            win = cfg.get("window_seconds", 3600)
            if not isinstance(win, int) or win < 1:
                raise ValueError("configuration.window_seconds must be an int >= 1")
            cfg.setdefault("window_seconds", win)

        elif ttype == "message_bus":
            if cfg.get("provider") not in ("redis", "kafka", "nats", "rabbitmq", "mqtt"):
                raise ValueError(
                    "configuration.provider must be redis/kafka/nats/rabbitmq/mqtt")
            if not isinstance(cfg.get("topic"), str) or not cfg["topic"].strip():
                raise ValueError("configuration.topic is required")

        elif ttype == "poll" and cfg.get("source") == "calendar":
            # sz-4.5c — calendar poll config has no url/kind; it is shaped
            # and validated by services.triggers.calendar_poll and gated
            # against a live connection in the service layer (create_trigger
            # / update_trigger). Nothing to check here.
            pass

        elif ttype == "poll":
            if cfg.get("kind") not in ("http", "rss"):
                raise ValueError("configuration.kind must be 'http' or 'rss'")
            url = cfg.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise ValueError("configuration.url must be an http(s) URL")
            from services.triggers.streaming import url_is_blocked
            blocked = url_is_blocked(url)
            if blocked:
                raise ValueError(f"configuration.url refused: {blocked}")
            secs = cfg.get("interval_seconds", 300)
            if not isinstance(secs, int) or secs < 30:
                raise ValueError("configuration.interval_seconds must be an int >= 30")
            cfg.setdefault("interval_seconds", secs)

        self.configuration = cfg
        return self


class WorkflowTriggerUpdate(BaseModel):
    """Update a workflow trigger.

    `trigger_type` cannot be changed after create. `configuration.secret_hash`
    cannot be set or modified by the client; the router strips it on patch.
    """
    configuration: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None

    @model_validator(mode='after')
    def _validate_configuration(self) -> 'WorkflowTriggerUpdate':
        """Validate configuration on PATCH the same way create does — without
        this, a user can overwrite a valid cron with garbage via PATCH and
        the scheduler then crashes at fire time. Only schedule (cron) keys
        need shape validation here; webhook/workflow_completion are no
        longer supported types so PATCH cannot recreate their config.
        """
        cfg = self.configuration
        if cfg is None:
            return self
        # If the caller supplies a cron field, enforce the same 5-field
        # shape rule the create validator uses. We don't know trigger_type
        # at this point (the router does), so we validate cron whenever
        # the key is present.
        cron = cfg.get("cron") if isinstance(cfg, dict) else None
        if cron is not None:
            if not isinstance(cron, str) or not cron.strip():
                raise ValueError(
                    "configuration.cron must be a non-empty string"
                )
            if len(cron.strip().split()) != 5:
                raise ValueError(
                    "configuration.cron must have 5 fields: "
                    "minute hour day month weekday"
                )
        tz = cfg.get("timezone") if isinstance(cfg, dict) else None
        if tz is not None and not isinstance(tz, str):
            raise ValueError("configuration.timezone must be a string")
        return self


class WorkflowTriggerResponse(BaseModel):
    """Workflow trigger response. Webhook secret_hash is stripped before
    serialization — clients never see the bcrypt hash or plaintext token.
    """
    id: int
    workflow_id: int
    tenant_id: int
    trigger_type: str
    configuration: Dict[str, Any]
    is_active: bool
    created_at: Optional[datetime] = None
    last_triggered_at: Optional[datetime] = None
    trigger_count: int = 0

    model_config = ConfigDict(from_attributes=True)

    @field_serializer('configuration')
    def _strip_webhook_secret(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(cfg, dict):
            return cfg
        if 'secret_hash' not in cfg and 'secret_token' not in cfg:
            return cfg
        clean = dict(cfg)
        clean.pop('secret_hash', None)
        clean.pop('secret_token', None)
        return clean


class WorkflowTriggerCreateResponse(WorkflowTriggerResponse):
    """Returned only on POST /road/triggers — webhook_secret + webhook_url
    are populated for `webhook` trigger creates and rotations, and are returned
    plaintext exactly once. All other trigger types return the same shape as
    WorkflowTriggerResponse with these fields set to None.
    """
    webhook_secret: Optional[str] = None
    webhook_url: Optional[str] = None


# ===============================================================
# tenant-resource-monitor
# ===============================================================

class TaskResourceAggregateRead(BaseModel):
    id: int
    task_id: int
    tenant_id: int
    user_id: Optional[int] = None
    gpu_seconds: float
    cpu_core_seconds: float
    ram_gb_seconds: float
    vram_gb_seconds: float
    disk_read_mb: float
    disk_write_mb: float
    wall_clock_seconds: float
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    aggregated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ResourceMonitorConfigRead(BaseModel):
    singleton_id: int
    gpu_seconds_per_credit_threshold: float
    cpu_core_seconds_per_credit_threshold: float
    ram_gb_seconds_per_credit_threshold: float
    io_mb_per_credit_threshold: float
    min_alert_credits: int
    enabled: bool
    updated_by_user_id: Optional[int] = None
    audit_event_id: Optional[int] = None
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ResourceMonitorConfigUpdate(BaseModel):
    """Platform-admin form payload. All fields optional — only
    changed knobs are submitted. Thresholds are in
    native-units-per-credit-billed."""
    gpu_seconds_per_credit_threshold: Optional[float] = Field(default=None, gt=0)
    cpu_core_seconds_per_credit_threshold: Optional[float] = Field(default=None, gt=0)
    ram_gb_seconds_per_credit_threshold: Optional[float] = Field(default=None, gt=0)
    io_mb_per_credit_threshold: Optional[float] = Field(default=None, gt=0)
    min_alert_credits: Optional[int] = Field(default=None, ge=0)
    enabled: Optional[bool] = None


class TenantThresholdOverride(BaseModel):
    """Per-tenant override stored as a JSONB row in tenant_settings.
    Any subset of the four per-resource thresholds may be overridden;
    omitted fields fall back to the platform default."""
    gpu_seconds_per_credit_threshold: Optional[float] = Field(default=None, gt=0)
    cpu_core_seconds_per_credit_threshold: Optional[float] = Field(default=None, gt=0)
    ram_gb_seconds_per_credit_threshold: Optional[float] = Field(default=None, gt=0)
    io_mb_per_credit_threshold: Optional[float] = Field(default=None, gt=0)


# Backwards-compat alias for the prior PUT body name during the
# de-dollarization transition. Callers that POST the legacy
# `{"resource_leak_threshold": N}` payload get rejected at the
# router level (no field accepted); they need to switch to the
# per-resource shape.
TenantThresholdUpdate = TenantThresholdOverride


class ResourceMonitorTenantRow(BaseModel):
    tenant_id: int
    tenant_name: str
    plan_name: Optional[str] = None
    gpu_seconds: float
    cpu_core_seconds: float
    ram_gb_seconds: float
    io_mb: float
    credits_used: int
    # Per-resource ratios — native_unit / credit_billed in the window.
    # Float('inf') is rendered as a large sentinel (1e9) for the JSON
    # surface so the UI can distinguish "ran with zero credits" from
    # a finite but extreme ratio.
    gpu_seconds_per_credit: float
    cpu_core_seconds_per_credit: float
    ram_gb_seconds_per_credit: float
    io_mb_per_credit: float
    task_count: int


class ResourceMonitorUserRow(BaseModel):
    user_id: Optional[int]
    user_email: Optional[str] = None
    gpu_seconds: float
    cpu_core_seconds: float
    ram_gb_seconds: float
    io_mb: float
    credits_used: int
    gpu_seconds_per_credit: float
    cpu_core_seconds_per_credit: float
    ram_gb_seconds_per_credit: float
    io_mb_per_credit: float
    task_count: int


class ResourceMonitorTaskRow(BaseModel):
    task_id: int
    node_id: Optional[str] = None
    workflow_run_id: Optional[int] = None
    gpu_seconds: float
    cpu_core_seconds: float
    ram_gb_seconds: float
    io_mb: float
    completed_at: Optional[datetime] = None


# Resolve forward references
CustomRole.model_rebuild()


# --- Persistent Stacks (add-persistent-stacks) ---
class StackHealth(BaseModel):
    """GA.ASW.4 — the platform's one-shot verdict on a stack's health.

    `grade` is one of `healthy` / `unhealthy` / `pending`. `reason` is set
    only for `unhealthy`, and only ever names why: a specific service's
    compose healthcheck/state, "no recent observation" when a supposedly-
    running stack has gone quiet past `observation_grace_seconds()`, or a
    bare stopped-container count when no per-service detail was reported."""
    grade: str
    reason: Optional[str] = None


class PersistentStackBase(BaseModel):
    name: str
    compose_spec: Dict[str, Any]
    storage_class: Literal["s3_native", "local_fs"] = "local_fs"
    home_zone: Optional[str] = None
    quota_bytes: Optional[int] = None

class PersistentStackCreate(PersistentStackBase):
    # Whether re-placing this stack elsewhere is CORRECT depends entirely on
    # what the service keeps, which only its author knows. Undeclared means
    # `external` — never auto-re-placed — because guessing wrong moves a
    # database.
    ha_class: Optional[Literal["stateless", "cache_backed",
                               "snapshot_restore", "external"]] = None
    # GA.ASW.6 — explicit "I understand this data is not backed up"
    # confirmation. Only consulted when `compose_spec` runs a known-database
    # image with no backup posture declared (see
    # `services.stack_logical_backup.backup_ack_required`); ignored otherwise.
    acknowledge_no_backup: bool = False

class PersistentStackUpdate(BaseModel):
    name: Optional[str] = None
    compose_spec: Optional[Dict[str, Any]] = None
    storage_class: Optional[Literal["s3_native", "local_fs"]] = None
    home_zone: Optional[str] = None
    quota_bytes: Optional[int] = None
    status: Optional[str] = None
    target_worker: Optional[str] = None
    lb_endpoint: Optional[str] = None
    storage_refs: Optional[Dict[str, Any]] = None
    desired_state: Optional[Literal["running", "stopped"]] = None
    ha_class: Optional[Literal["stateless", "cache_backed",
                               "snapshot_restore", "external"]] = None

class PersistentStack(PersistentStackBase):
    id: int
    tenant_id: int
    status: str
    # Why the stack is in its current status when something went wrong, set
    # from the worker task's reported error. None on a healthy stack.
    status_reason: Optional[str] = None
    # Teardown was dispatched but never confirmed — resources may survive on
    # the host. What a secondary cleanup sweep looks for.
    cleanup_pending: bool = False
    # WS4.Q8 — true when this payload is an on-ramp PREVIEW (from-template
    # with preview=true): the compose was rendered + validated but no
    # PersistentStack row was persisted; `id` is 0 in that case.
    preview: bool = False
    # Per-service accelerator declarations parsed from the compose spec's
    # x-mod.accelerator fields. Requirements only — where they get satisfied is
    # a just-in-time claim-time decision, not a stored placement.
    accelerator_requirements: Optional[Dict[str, Any]] = None
    # Per-service egress posture parsed from x-mod.egress (delegated-stack-
    # authoring §4). {service: {mode: none|api|hosts, hosts?: [...]}}. Recorded,
    # not yet enforced. None = nothing declared.
    egress_policy: Optional[Dict[str, Any]] = None
    target_worker: Optional[str] = None
    lb_endpoint: Optional[str] = None
    # Per-service overlay identity for reachable services (mod-connect-service-mesh
    # §2): {service: {magicdns_name, node_id, status}}. None on stacks with no
    # reachable service.
    mesh_endpoints: Optional[Dict[str, Any]] = None
    # Per-service secret references parsed from x-mod.secrets (stack-secrets-and-
    # certs §1): {service: {env: vault-path}}. References only, never values.
    secret_refs: Optional[Dict[str, Any]] = None
    # Per-service TLS declaration parsed from x-mod.tls (stack-secrets-and-certs
    # §5): {service: {mode: auto|byo, fqdn, not_after?}}. The byo cert+key live in
    # Vault, never here — this is only the declaration plus the parsed expiry.
    tls_config: Optional[Dict[str, Any]] = None
    # §5.3 — "expires in N days" warnings for byo certs nearing expiry, computed
    # on read from tls_config.not_after. None/empty when nothing is close.
    tls_warnings: Optional[List[str]] = None
    # caddy-stack-ingress §2 — PUBLIC exposure (opt-in; default internal-only).
    # `exposed` is the magic-URL switch; `public_hostname` the computed MOD-run
    # FQDN (t<tid>-<stackid>.apps.<BASE_DOMAIN>) shown while exposed; `custom_domain`
    # an optional BYO domain. The UI (stacks list / card) reads these to show a
    # stack's front door; None/false on an internal-only stack.
    exposed: bool = False
    public_hostname: Optional[str] = None
    custom_domain: Optional[str] = None
    # friendly-stack-fqdn (mesh-native-llm-routing D4) — an optional human-friendly
    # public alias for an exposed stack, aliased to its stable internal mesh name.
    # None on an internal-only stack. `edge_target` is the public edge hostname the
    # customer points their OWN DNS (CNAME/A) at — informational only; MOD
    # provisions no customer DNS. Derived on read; None when not exposed.
    friendly_fqdn: Optional[str] = None
    edge_target: Optional[str] = None
    storage_refs: Optional[Dict[str, Any]] = None
    # What the operator WANTS, as distinct from `status` (what is happening)
    # and from observation (what is actually running). A stack deliberately
    # stopped is not a failure, and reconciliation must not fight the operator
    # who stopped it.
    desired_state: str = "running"
    # 30.6.15 cUI-2a — the keep-running floor (30.6.7 c7-a1: models.py:8563
    # `keep_running`, TRUE = idle reclaim skips this stack; c7-a2: PUT
    # /stacks/{id}/keep-running sets it). Read here so the UI can show the state.
    keep_running: bool = False
    # Whether automatic re-placement is correct for this stack. `external`
    # never re-places — MOD's only job there is not running two primaries.
    ha_class: str = "external"
    # GR.FEN.3a — why this stack has its ha_class: the inference reason
    # (e.g. "service 'llm' mounts model-cache host path '/var/lib/mod/hf-cache'")
    # set by the create path when no explicit class was given, or
    # "explicitly set at create". Not a stored column — the stack model has no
    # JSONB metadata field, so it is returned only where the API knows it
    # (create response) and is None elsewhere. None = class set by an older
    # path that predates inference (or the pre-inference default).
    ha_class_reason: Optional[str] = None
    # GR.FEN.1b — what the stack's CURRENT host last observed its containers'
    # ha_class label to be (from the worker heartbeat), and whether it
    # disagrees with the DB class above. Editing ha_class only reaches the
    # running containers on the next redeploy, and the worker fences on the
    # LABEL — so a mismatch means the stored class is not yet in effect.
    # Computed on read (`services.stack_observation.ha_class_state`);
    # observed is None when no fresh observation carries a label, and
    # mismatch is False in that case (nothing has been observed).
    ha_class_observed: Optional[str] = None
    ha_class_mismatch: bool = False
    ha_class_mismatch_note: Optional[str] = None
    # 13.5e — a secret this stack references was rotated (value changed), so the
    # running containers still hold the OLD value. Set on rotation, cleared by a
    # successful deploy. The Dashboard shows a "Restart needed" badge on the
    # stack row / card with a one-click Redeploy while this is true.
    needs_redeploy: bool = False
    # §8.1 — the host this stack is fixed to, if any. On the list response
    # because "why does this one never move" is asked while looking at the list.
    pinned_worker: Optional[str] = None
    # The note travels with the pin. A pin outlives whoever set it, and the
    # question it provokes is always "why can this one never move" — asked
    # while looking at the list, so the answer has to be there too.
    pinned_reason: Optional[str] = None
    # Group R 27.5a — the freshest LLM-gateway (SMG) queue-metric snapshot this
    # stack's workers observed on a recent beat: {replicas_total,
    # replicas_healthy, queue_depth, per_worker:[{url,healthy,active}],
    # wait_p50_s, wait_p95_s, cache_hit_rate}. None for non-LLM stacks, and for
    # LLM stacks whose workers never scraped their gateway yet (or that are
    # stopped — a down stack has no live queue).
    gateway_metrics: Optional[Dict[str, Any]] = None
    # Workers this stack can NEVER run on — wrong GPU kind, too few cards, cards
    # too small — mapped to the reason. The worker picker greys these out with
    # the reason rather than letting a pin produce a stack that is unplaceable
    # forever. Busy-but-capable workers are not listed; they may free up.
    hardware_ineligible: Dict[str, str] = {}
    # §9.2 — a stack with no backup schedule must be distinguishable from one
    # with a schedule, IN THE LIST. Per-stack detail lives at
    # GET /{id}/backups, but a user scanning their stacks for the unprotected
    # one should not have to open each in turn to find it. None means no
    # schedule has been set, which is not the same as 0 (deliberately none).
    backup_interval_hours: Optional[int] = None
    backup_last_run_at: Optional[datetime] = None
    backup_last_error: Optional[str] = None
    # persistent-stack-billing §2/§5.4 — the priced reservation vector snapshotted
    # at deploy ({cpu_cores, mem_gb, gpu:[vram_gb...]}) and the meter anchor. Null
    # on a stack that has not deployed since billing shipped, or one not currently
    # metered. reservation_cost_per_hour is derived on read (units × current
    # rates) so the stack card can show "N credits/hour"; it is 0 while metering
    # is dark (rates 0), which is the honest pre-launch number.
    reservation_units: Optional[Dict[str, Any]] = None
    metered_through_at: Optional[datetime] = None
    reservation_cost_per_hour: Optional[float] = None
    # GA.ASW.4 — the platform's own read on whether this stack is well:
    # healthy / unhealthy (+ reason) / pending. Computed on read from
    # worker-reported observation (`services.stack_observation.
    # stack_health_grade`); None only if the computation itself raised, so a
    # bug in health grading can never take the stack list down with it.
    health: Optional["StackHealth"] = None
    # 30.6.13 c13-J3 — which App the stack's running identity token belongs to.
    # Derived on read from the mint record storage_refs["identity"]; never the
    # token itself.
    identity_app_id: Optional[int] = Field(None, description="App id the running stack's identity token was minted for; null = stack-only identity or never minted")
    identity_minted_at: Optional[str] = Field(None, description="ISO-8601 Z mint time")
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Wizard Queue Schemas (wizard-queue-redesign) ---
from models import (  # noqa: E402
    WizardRunType,
    WizardRunStatus,
    WizardRunOrigin,
)


class WizardRunResponse(BaseModel):
    """One Wizard Queue row / run detail — the shared wire contract
    rendered identically by the WorkflowBuilder panel and the Dashboard
    System -> Activity view."""
    id: str
    wizard_type: WizardRunType
    target_name: Optional[str] = None
    target_ref_type: Optional[str] = None
    target_ref_id: Optional[str] = None
    status: WizardRunStatus
    progress: int = 0
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    error_message: Optional[str] = None
    owner_user_id: Optional[int] = None
    tenant_id: Optional[int] = None
    is_platform_level: bool = False
    origin: WizardRunOrigin
    retry_of_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    log_source: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    # Whether the current caller may cancel / retry this run (defence-in
    # -depth hint for the UI; server still enforces on the endpoints).
    can_cancel: bool = False
    can_retry: bool = False

    model_config = ConfigDict(from_attributes=True)


class WizardRunListResponse(BaseModel):
    runs: List[WizardRunResponse]
    total_count: int


class WizardRunLogLine(BaseModel):
    seq: int
    level: Optional[str] = None
    message: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WizardRunLogsResponse(BaseModel):
    run_id: str
    lines: List[WizardRunLogLine]
    # Highest seq returned — the client passes this back as ?after_seq=
    # to poll only for newer lines (polling fallback for the SSE tail).
    last_seq: int


# --- AI slot bindings (ai-config-stack-binding §6.1) ---
# The DTOs behind Settings -> AI: "Who uses what" (one row per slot: what
# serves it and WHERE that answer came from) and "AI stacks" (every LLM
# stack: model, running/sleeping/down, live health, and the slots bound to
# it). `key_ref` is NEVER in any response — see AiSlotBindingRow.key_ref_has_
# key, which is the same bool-for-secret convention ToolAIConfig.api_key
# already uses (ai_config.py:1626 / :1993).
AiSlotNameType = Literal["wizard", "adapter", "assistant", "support",
                         "repair", "critic", "wizard_learning_embed",
                         "assistant_user_rag_embed",
                         "assistant_knowledge_embed"]
"""The nine bindable AI roles (models.py ck_ai_slot_binding_slot)."""


class AiBindingTarget(BaseModel):
    """What one slot's answer comes from — the resolver's `target` block.

    Never a guess: absent when nothing was ever picked for the slot. `key_ref`
    is deliberately NOT a field here (design D1/D7: it is a Vault PATH and the
    raw key lives there); the caller gets `key_ref_has_key` instead."""
    slot: Optional[AiSlotNameType] = None
    capability: Optional[Literal["chat", "embedding"]] = None
    # 'platform' | 'tenant' — WHICH row answered, i.e. the source of precedence
    # (Q-AI2 = A: a tenant row overrides the platform row for that slot).
    source: Optional[Literal["platform", "tenant"]] = None
    priority: Optional[int] = None
    stack_id: Optional[int] = None
    stack_name: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    key_ref_has_key: bool = False


class AiSlotResolution(BaseModel):
    """One slot's typed outcome — the resolver's whole contract (design D2).

    `outcome` is the discriminated union; every non-`ready` value carries a
    `message` naming the cause AND the remedy, so the page never shows a blank
    "not configured" for a stack that is merely asleep."""
    slot: AiSlotNameType
    capability: Literal["chat", "embedding"]
    outcome: Literal["ready", "no_binding", "stack_stopped", "stack_missing",
                     "unreachable", "waking"]
    message: str = ""
    target: Optional[AiBindingTarget] = None
    # The slot's ordered fallback chain (owner BUG.B56): one entry per row
    # tried, in priority order, each with ITS OWN cause — so "primary stopped,
    # fallback missing" is one read, never a silent swap (Q-AI1 = B).
    attempts: List[Dict[str, Any]] = []
    # `waking` only: what the wake has already done, so the UI can show
    # "Starting the AI model..." and retry.
    progress: Optional[str] = None


class AiBindingsListResponse(BaseModel):
    """GET /platform/ai-config/bindings — every slot, effective + source."""
    rows: List[AiSlotResolution]
    # Slots that have NO row at any scope (nobody ever picked). The page
    # renders these as "no AI model is bound", which is a different sentence
    # from a slot whose binding exists and failed.
    unbound_slots: List[AiSlotNameType] = []


class AiBindingUpdate(BaseModel):
    """PUT body for one slot's binding (platform or tenant scope).

    Partial like every other update endpoint here: only what the caller sends
    is touched. `enabled=false` is the SOFT delete (ISO 27001 A.18.1.3) — the
    row is stamped `retired_at` and never hard-deleted, so "this slot once ran
    on that stack" survives. Reviving a retired position writes a NEW row
    (the table's own rule)."""
    target_kind: Optional[Literal["stack", "external", "auto"]] = None
    stack_id: Optional[int] = None
    # The exact served-model name when a stack serves several.
    served_model: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    # A Vault PATH (e.g. platform/ai-config/assistant) — never a secret, and
    # never returned by any endpoint (only `key_ref_has_key` is).
    key_ref: Optional[str] = None
    # 0 = primary, 1..15 = the ordered fallbacks tried after a failure.
    priority: int = 0
    enabled: bool = True


class AiBindingResult(BaseModel):
    """What a PUT actually did — the row's new shape, masked the same way."""
    slot: AiSlotNameType
    scope: Literal["platform", "tenant"]
    tenant_id: Optional[int] = None
    priority: int
    target_kind: Literal["stack", "external", "auto"]
    stack_id: Optional[int] = None
    stack_name: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    capability: Literal["chat", "embedding"]
    enabled: bool
    key_ref_has_key: bool = False
    # The row's position in the chain after the write, plus the resolution it
    # now produces, so the operator sees the effect of their pick immediately.
    resolution: Optional[AiSlotResolution] = None
    message: str = ""


class AiStackRow(BaseModel):
    """One LLM stack in GET /platform/ai-config/ai-stacks."""
    stack_id: int
    name: str
    status: str
    # 'chat' | 'embedding' — what kind of model this stack serves
    # (services/ai_stack_provider.stack_kind).
    kind: Literal["chat", "embedding"]
    model: Optional[str] = None
    served_models: List[str] = []
    # The stack's own compose-level health verdict (the same shape the
    # Persistent Stacks page shows, schemas.StackHealth).
    health: Optional["StackHealth"] = None
    # Live answer for THIS stack from build_managed_llm_health (ai_config_service
    # :571): is its server answering right now, and what it reports serving.
    live_healthy: Optional[bool] = None
    live_served_models: List[str] = []
    live_error: Optional[str] = None
    # The slots bound to this stack, primary first — "used by" (design D1).
    used_by: List[AiSlotResolution] = []
    # Every binding row pointing at this stack, INCLUDING retired ones, so the
    # history of a stack stays visible (soft delete only).
    binding_count: int = 0
    retired_binding_count: int = 0


class AiStacksListResponse(BaseModel):
    """GET /platform/ai-config/ai-stacks — every LLM stack, with its slots."""
    stacks: List[AiStackRow]
    # The compose-level vLLM verdict for the platform as a whole
    # (build_managed_llm_health, ai_config_service.py:571), so the page can
    # say "the managed server is down" once instead of per stack. Shaped
    # exactly like ManagedLLMHealthResponse (routers/ai_config.py:2571) —
    # dumped to a dict here because that DTO lives in the router.
    managed_health: Optional[Dict[str, Any]] = None


# --- assistant knowledge-source bindings (owner BUG.B59b) ---
# The DTOs behind the assistant's knowledge PICKER: MOD Docs, a tenant's own
# files, and tenant uploads each get their OWN embedding model (BUG.B59 Q-A
# = A). Same conventions as the slot bindings directly above: `key_ref` is a
# Vault PATH and is NEVER returned — only `key_ref_has_key` is — and a
# retired row is soft (enabled=false + retired_at), never hard-deleted.
AiKnowledgeSourceKindType = Literal["mod_docs", "tenant_files", "uploads"]
"""The three knowledge sources the picker offers (models.py ck_akb_source_kind)."""


class AiKnowledgeSourceBindingRow(BaseModel):
    """One source's live binding row — masked, never the raw Vault PATH."""
    scope: Literal["platform", "tenant"]
    tenant_id: Optional[int] = None
    source_kind: AiKnowledgeSourceKindType
    target_kind: Literal["stack", "external", "auto"]
    stack_id: Optional[int] = None
    stack_name: Optional[str] = None
    served_model: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    # 'embedding' always — a knowledge source is embedded, never chatted
    # (services/ai_slot_resolver.py EMBEDDING_SLOTS).
    capability: Literal["embedding"] = "embedding"
    enabled: bool
    key_ref_has_key: bool = False
    retired_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AiKnowledgeSourceRow(BaseModel):
    """One knowledge source in GET /platform/ai-config/knowledge-sources."""
    source_kind: AiKnowledgeSourceKindType
    # The platform's row for this kind, or None (nobody ever bound it).
    platform_binding: Optional[AiKnowledgeSourceBindingRow] = None
    # This tenant's override, when the caller has a tenant and the tenant has
    # a row for this kind (live OR retired — a retired override is shown so the
    # page can say "this tenant turned this source off", 5.1-style).
    tenant_binding: Optional[AiKnowledgeSourceBindingRow] = None
    # Which row actually answers for the caller: 'tenant' overrides 'platform';
    # None when nothing answers at all.
    effective_scope: Optional[Literal["platform", "tenant"]] = None
    # The effective row's shape again, for the page's one-line "served by".
    effective_binding: Optional[AiKnowledgeSourceBindingRow] = None
    # True when NO live row exists at either scope for this kind.
    unbound: bool = False


class AiKnowledgeSourcesListResponse(BaseModel):
    """GET /platform/ai-config/knowledge-sources — every source, effective + source."""
    rows: List[AiKnowledgeSourceRow]
    # Kinds with no live row at either scope (nobody ever picked) — the page
    # renders these as "no embedding model is bound", a different sentence from
    # a source whose binding exists and is merely failing.
    unbound_source_kinds: List[AiKnowledgeSourceKindType] = []


class AiKnowledgeSourceBindingUpdate(BaseModel):
    """PUT body for one source's binding (platform or tenant scope).

    Partial like every other update endpoint here: only what the caller sends
    is touched. `enabled=false` is the SOFT delete (ISO 27001 A.18.1.3) — the
    row is stamped `retired_at` and never hard-deleted, so "this tenant once
    embedded its uploads with that model" survives. Reviving a retired source
    writes a NEW row (the table's own partial-unique rule)."""
    target_kind: Optional[Literal["stack", "external", "auto"]] = None
    stack_id: Optional[int] = None
    # The exact served-model name when a stack serves several.
    served_model: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    # A Vault PATH (e.g. platform/ai-config/knowledge-embed) — never a secret,
    # and never returned by any endpoint (only `key_ref_has_key` is).
    key_ref: Optional[str] = None
    enabled: bool = True


class AiKnowledgeSourceBindingResult(BaseModel):
    """What a knowledge-source PUT actually did — the row's new shape, masked."""
    source_kind: AiKnowledgeSourceKindType
    scope: Literal["platform", "tenant"]
    tenant_id: Optional[int] = None
    target_kind: Literal["stack", "external", "auto"]
    stack_id: Optional[int] = None
    stack_name: Optional[str] = None
    served_model: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    capability: Literal["embedding"] = "embedding"
    enabled: bool
    key_ref_has_key: bool = False
    retired_at: Optional[datetime] = None
    message: str = ""

# --- goal-directed planner SSE + pause/resume schemas ---
PlannerSSEEventType = Literal["plan_started", "step_specialized", "needs_user_choice",
                              "plan_completed", "error"]


class PlannerStepOption(BaseModel):
    """One tier pick for a skeleton step (budget / balanced / premium)."""
    option_id: str
    tier: str
    node_definition_id: int
    node_kind: Optional[str] = None
    estimated_cost_credits: int
    estimated_latency_seconds: Optional[int] = None
    quality_score: Optional[float] = None
    score: float
    over_budget: bool = False


class PlannerStepOptions(BaseModel):
    step_id: str
    category: str
    needs_user_choice: bool = False
    options: List[PlannerStepOption] = []


class NeedsUserChoiceEvent(BaseModel):
    """SSE payload emitted when the planner pauses for a tiered choice."""
    event: Literal["needs_user_choice"] = "needs_user_choice"
    session_id: str
    plan_id: Optional[str] = None
    tenant_id: int
    step_id: str
    category: str
    options: List[PlannerStepOption] = []
    message: str = ""


class PlannerGraphNode(BaseModel):
    step_id: str
    category: str
    node_definition_id: int
    node_kind: Optional[str] = None
    estimated_cost_credits: int
    estimated_latency_seconds: Optional[int] = None
    quality_score: Optional[float] = None
    budget_exceeded: bool = False


class PlannerSessionState(BaseModel):
    session_id: str
    plan_id: Optional[str] = None
    tenant_id: int
    status: Literal["paused", "completed"]
    paused_step_id: Optional[str] = None
    pending_options: Optional[List[PlannerStepOption]] = None
    graph_nodes: List[PlannerGraphNode] = []
    edges: List[List[str]] = []
    event: Optional[NeedsUserChoiceEvent] = None


class PlannerSessionCreate(BaseModel):
    steps: List[Dict[str, Any]]
    edges: List[List[str]] = []
    max_cost_credits: Optional[int] = None
    max_latency_seconds: Optional[int] = None
    min_quality: Optional[float] = None


class PlannerChoice(BaseModel):
    option_id: str

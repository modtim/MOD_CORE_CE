# MOD Tech Labs - CRUD Module Refactoring

## Overview

The CRUD layer is the `CORE/API/crud/` package, organized by business domain. This improves maintainability, testability, and makes it easier to find relevant CRUD operations.

## New Structure

```
CORE/API/crud/
│   ├── __init__.py                # Re-exports all CRUD functions
│   ├── tenant_cud.py              # Tenant management and provisioning
│   ├── user_cud.py                # User management
│   ├── cog_cud.py                 # Cog (folder/file) management
│   ├── api_key_cud.py             # API key management
│   ├── permission_cud.py          # Permission management
│   ├── invitation_cud.py          # Invitation management
│   ├── secret_cud.py              # Secret management
│   ├── workflow_cud.py            # Workflow and version management
│   ├── team_cud.py                # Team management
│   ├── resource_lock_cud.py       # Resource locking mechanism
│   ├── webhook_cud.py             # Webhook endpoint management
│   ├── tag_cud.py                 # Tag management
│   ├── subscription_cud.py        # Subscription management
│   ├── node_definition_cud.py     # Node definition management
│   ├── drive_cud.py               # Drive integration management
│   ├── file_cud.py                # File record management
│   ├── node_run_cud.py            # Node run management
│   ├── smtp_cud.py                # SMTP settings management
│   ├── role_cud.py                # Custom role management
│   └── shareable_link_cud.py      # Shareable link management
```

## Module Breakdown

### `tenant_cud.py` - Tenant Management
**Functions:**
- `create_tenant_and_parent_user()` - Create tenant with parent user
- `create_tenant_with_default_roles()` - Create tenant with default roles

**Purpose:** Tenant provisioning and setup, including MinIO bucket creation, Keycloak group setup, and system node definition provisioning.

### `user_cud.py` - User Management
**Functions:**
- `get_user()` - Get user by ID
- `get_user_by_keycloak_id()` - Get user by Keycloak ID
- `create_user()` - Create new user
- `get_child_users()` - Get users with a specific parent
- `get_users_in_tenant()` - Get all users in a tenant
- `count_active_users_in_tenant()` - Count active user seats
- `get_user_in_tenant()` - Get user by ID within tenant context
- `get_user_by_id_for_tenant()` - Get user with tenant security check
- `update_user()` - Update user properties
- `update_user_profile()` - Update user profile details
- `deactivate_user()` - Deactivate user account
- `delete_user_permanently()` - Delete user (hard delete)
- `get_user_teams()` - Get teams a user belongs to
- `get_user_assigned_tasks()` - Get assigned intervention tasks

**Purpose:** User lifecycle management, tenant-scoped user operations, and user关系 management.

### `cog_cud.py` - Cog Management
**Functions:**
- `create_cog()` - Create new cog (folder/file)
- `get_cogs()` - List cogs with filtering/pagination
- `get_cog_by_id()` - Get cog by ID with owner check
- `get_cog_by_id_and_tenant()` - Get cog by ID within tenant
- `update_cog()` - Update cog properties
- `delete_cog()` - Soft delete cog
- `soft_delete_cogs_recursive()` - Recursive soft delete with audit protection

**Purpose:** Cog (folder/file) CRUD operations with soft deletion, tenant isolation, and audit protection for locked folders.

### `api_key_cud.py` - API Key Management
**Functions:**
- `create_api_key()` - Create new API key
- `get_api_key_by_key_prefix()` - Get API key by prefix
- `get_api_key_by_keycloak_client_id()` - Get API key by Keycloak client
- `get_api_keys_for_user()` - List user's API keys
- `get_user_api_key()` - Get specific user's API key
- `revoke_api_key()` - Revoke API key (soft delete)

**Purpose:** API key lifecycle management with tenant isolation and Keycloak integration.

### `permission_cud.py` - Permission Management
**Functions:**
- `check_user_permission_on_cog()` - Check if user has permission on cog
- `get_permissions_for_resource()` - List permissions for resource
- `grant_permission()` - Create new permission grant
- `get_permission_by_id()` - Get permission by ID
- `revoke_permission()` - Revoke permission (soft delete)

**Purpose:** Permission checking and management with inheritance support and tenant isolation.

### `invitation_cud.py` - Invitation Management
**Functions:**
- `create_invitation()` - Create new invitation
- `accept_invitation()` - Accept invitation
- `get_invitations_for_tenant()` - List tenant invitations
- `get_invitation_by_id_for_tenant()` - Get invitation in tenant context
- `get_invitation_by_id()` - Get invitation globally
- `delete_invitation()` - Delete invitation
- `get_pending_invitations_for_tenant()` - List pending invitations
- `get_pending_invitation_by_token()` - Get invitation by token

**Purpose:** Invitation lifecycle management with tenant isolation and automated permission granting.

### `secret_cud.py` - Secret Management
**Functions:**
- `create_secret()` - Create encrypted secret
- `get_decrypted_secret()` - Retrieve decrypted secret
- `get_secrets_for_tenant()` - List secrets in tenant
- `get_secret_by_id()` - Get secret by ID
- `get_secret_by_name()` - Get secret by name
- `get_decrypted_secret_by_name()` - Get and decrypt by name
- `update_secret()` - Update secret
- `delete_secret()` - Soft delete secret

**Purpose:** Secret management with tenant-specific encryption keys and secure decryption.

### `workflow_cud.py` - Workflow Management
**Functions:**
- `create_workflow()` - Create workflow container
- `get_workflows_for_tenant()` - List tenant workflows
- `get_workflow_by_id()` - Get workflow in tenant context
- `update_workflow()` - Update workflow
- `delete_workflow()` - Soft delete workflow
- `create_workflow_version()` - Create workflow version
- `get_workflow_versions()` - List workflow versions
- `get_workflow_version_by_id()` - Get version in tenant context
- `get_or_create_project_workflows_folder()` - Ensure workflows folder exists
- `create_workflow_run_folder()` - Create workflow run directory
- `create_workflow_step_folder()` - Create workflow step directory

**Purpose:** Workflow lifecycle management with versioning and run folder organization.

### `team_cud.py` - Team Management
**Functions:**
- `create_team()` - Create new team
- `get_teams()` - List tenant teams
- `get_team()` - Get team in tenant context
- `add_team_member()` - Add user to team

**Purpose:** Team management for user collaboration within tenants.

### `resource_lock_cud.py` - Resource Locking
**Functions:**
- `get_active_locks_for_tenant()` - List active locks in tenant
- `get_active_lock_for_resource()` - Get lock for specific resource
- `acquire_lock()` - Acquire resource lock
- `release_lock()` - Release resource lock

**Purpose:** Resource locking mechanism to prevent concurrent modifications.

### `webhook_cud.py` - Webhook Management
**Functions:**
- `create_webhook_endpoint()` - Create webhook endpoint
- `get_webhook_endpoints()` - List tenant webhooks
- `get_webhook_endpoint()` - Get webhook in tenant context
- `delete_webhook_endpoint()` - Delete webhook endpoint

**Purpose:** Webhook endpoint management with signing secrets for security.

### `tag_cud.py` - Tag Management
**Functions:**
- `create_tag()` - Create new tag
- `get_tags_for_tenant()` - List tenant tags
- `get_tags_for_cog()` - Get tags on specific cog
- `add_tag_to_cog()` - Associate tag with cog
- `remove_tag_from_cog()` - Remove tag from cog

**Purpose:** Tag management for organizing and categorizing resources.

### `subscription_cud.py` - Subscription Management
**Functions:**
- `get_subscription_for_tenant()` - Get tenant subscription
- `update_subscription_after_payment()` - Update after payment
- `update_subscription_status()` - Update subscription status

**Purpose:** Subscription lifecycle management with Stripe integration.

### `node_definition_cud.py` - Node Definition Management
**Functions:**
- `create_node_definition()` - Create node definition
- `get_node_definitions()` - List tenant node definitions
- `get_node_definition_by_id()` - Get node definition
- `update_node_definition()` - Update node definition
- `delete_node_definition()` - Delete node definition
- `get_node_definition_by_name()` - Get node definition by name

**Purpose:** Node definition management with system-level and tenant-level definitions.

### `drive_cud.py` - Drive Integration Management
**Functions:**
- `create_drive()` - Create new drive
- `get_drive_by_id()` - Get drive by ID
- `get_drives_by_owner()` - List owner's drives
- `update_drive()` - Update drive
- `delete_drive()` - Delete drive

**Purpose:** External drive integration management.

### `file_cud.py` - File Record Management
**Functions:**
- `create_file_record()` - Create file record
- `get_file_record()` - Get file record
- `create_workflow_run_file()` - Link file to workflow run
- `get_workflow_run_files()` - List workflow files
- `get_workflow_run_file_by_id()` - Get file in workflow run
- `update_workflow_run_file()` - Update workflow file
- `delete_workflow_run_file()` - Delete workflow file

**Purpose:** File record management with workflow run integration.

### `node_run_cud.py` - Node Run Management
**Functions:**
- `get_node_runs_for_workflow_run()` - List node runs
- `get_node_run_by_id()` - Get node run in tenant context
- `update_node_run_status()` - Update node run status
- `get_workflow_runs()` - List workflow runs
- `get_workflow_run_by_id()` - Get workflow run

**Purpose:** Node run tracking and workflow execution management.

### `smtp_cud.py` - SMTP Settings Management
**Functions:**
- `get_smtp_settings_by_user_id()` - Get SMTP settings
- `create_smtp_settings()` - Create SMTP settings
- `update_smtp_settings()` - Update SMTP settings
- `delete_smtp_settings()` - Delete SMTP settings

**Purpose:** SMTP configuration management with encrypted passwords.

### `role_cud.py` - Custom Role Management
**Functions:**
- `create_custom_role()` - Create custom role
- `get_custom_roles()` - List tenant roles
- `get_custom_role()` - Get role in tenant context
- `update_custom_role()` - Update role
- `delete_custom_role()` - Soft delete role

**Purpose:** Custom role management with capability-based permissions.

### `shareable_link_cud.py` - Shareable Link Management
**Functions:**
- `soft_delete_shareable_link()` - Soft delete link
- `get_shareable_link_for_owner()` - Get link in owner context
- `create_shareable_link()` - Create shareable link
- `get_shareable_links_for_cog()` - List cog shareable links

**Purpose:** Shareable link management with password protection and expiration.

## Migration Guide

### Before (Old Import)
```python
from api.crud import create_user, get_cogs, grant_permission
```

### After (New Import)
```python
from api.crud import create_user, get_cogs, grant_permission
# The same import path still works - modules are re-exported!
```

### Direct Module Import (Optional)
```python
from api.crud.user_cud import create_user
from api.crud.cog_cud import get_cogs
from api.crud.permission_cud import grant_permission
```

## Benefits of This Refactoring

1. **Easier Navigation:** Developers can quickly find CRUD operations for specific domains
2. **Better Testability:** Each module can be tested independently
3. **Clearer Ownership:** Teams can own specific CRUD modules
4. **Reduced Merge Conflicts:** Smaller files mean fewer conflicts
5. **Improved Maintainability:** Easier to understand and modify
6. **Tenant-First Design:** Clear tenant isolation in function signatures
7. **Security by Convention:** Tenant ID validation is consistent across modules

## Tenant Isolation Pattern

All CRUD functions follow a consistent tenant isolation pattern:

```python
def some_function(db: Session, tenant_id: int, user_id: int, ...):
    """Function operates within tenant context"""
    # All queries use tenant_id for isolation
    query = db.query(Model).filter(
        Model.tenant_id == tenant_id,
        Model.user_id == user_id
    )
```

## Migration Status

✅ Completed Modules:
- Tenant management
- User management
- Cog management
- API key management
- Permission management
- Invitation management
- Secret management
- Workflow management
- Team management
- Resource locking
- Webhook management
- Tag management
- Subscription management
- Node definition management
- Drive management
- File management
- Node run management
- SMTP settings management
- Custom role management
- Shareable link management

## Next Steps

1. **Run Database Migrations:** Ensure all database changes are applied
2. **Update Tests:** Modify tests to use the new module structure
3. **Update API Documentation:** Document the new function signatures
4. **Monitor for Breaking Changes:** Check logs for any import issues
5. **Gradual Migration:** Consider gradual migration of dependent code

## Backward Compatibility

The refactored `api/crud.py` re-exports all functions, so existing imports continue to work:

```python
# These imports still work:
from api.crud import create_user, get_cogs, grant_permission

# But you can also use the new structure:
from api.crud.user_cud import create_user
from api.crud.cog_cud import get_cogs
from api.crud.permission_cud import grant_permission
```

## Contributing Guidelines

When adding new CRUD operations:

1. **Choose the right module:** Place new functions in the most relevant module
2. **Follow tenant isolation:** Always include `tenant_id` in function signatures
3. **Use consistent naming:** Follow the verb-noun pattern (e.g., `create_user`, `get_cogs`)
4. **Add proper type hints:** Use SQLAlchemy models and Pydantic schemas
5. **Include docstrings:** Document parameters, return values, and purpose
6. **Test thoroughly:** Add unit tests for new CRUD operations
7. **Follow security patterns:** Ensure proper tenant and user validation

---

**For questions or issues, contact the MOD Tech Labs development team.**

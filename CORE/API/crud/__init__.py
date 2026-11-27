"""Core CRUD operations - Import all CRUD modules.

This module re-exports all CRUD functions from the organized submodules.
Use this as a drop-in replacement for the old monolithic crud.py file.

Example usage:
    from api.crud import (
        get_user, create_user,  # user_cud module
        get_cogs, create_cog,  # cog_cud module
        grant_permission,  # permission_cud module
        # ... etc
    )
"""

# Import all CRUD modules for re-export using relative imports
from .tenant_cud import (
    create_tenant_and_parent_user,
    create_tenant_with_default_roles,
    ensure_tenant_system_roles,
    SYSTEM_ROLE_NAMES,
    trigger_invitation_dag,
)

from .user_cud import (
    get_user,
    get_user_by_keycloak_id,
    create_user,
    get_child_users,
    get_users_in_tenant,
    count_active_users_in_tenant,
    get_user_in_tenant,
    get_user_by_id_for_tenant,
    update_user,
    update_user_profile,
    deactivate_user,
    reactivate_user,
    delete_user_permanently,
    get_user_teams,
    get_user_assigned_tasks,
    get_user_by_api_key
)

from .app_cud import (
    get_revived_app,
)

from .cog_cud import (
    create_cog,
    get_cogs,
    get_cog_by_id,
    get_cog_by_id_and_tenant,
    update_cog,
    delete_cog,
    soft_delete_cogs_recursive,
    create_cog_version,
    get_cog_versions
)

from .api_key_cud import (
    create_api_key,
    get_api_key_by_key_prefix,
    get_api_key_by_keycloak_client_id,
    get_api_keys_for_user,
    get_user_api_key,
    revoke_api_key
)

from .invitation_cud import (
    create_invitation,
    accept_invitation,
    get_invitations_for_tenant,
    get_invitation_by_id_for_tenant,
    get_invitation_by_id,
    delete_invitation,
    get_pending_invitations_for_tenant,
    get_pending_invitation_by_token
)

from .secret_cud import (
    create_secret,
    create_or_update_secret,
    get_decrypted_secret,
    get_secrets_for_tenant,
    get_secret_by_id,
    get_secret_by_name,
    get_decrypted_secret_by_name,
    update_secret,
    delete_secret,
    resolve_secret_for_tenant,
    resolve_decrypted_secret_for_tenant,
)

from .workflow_cud import (
    create_workflow,
    create_workflow_run,
    get_workflows_for_tenant,
    get_workflow_by_id,
    update_workflow,
    delete_workflow,
    set_workflow_archived,
    duplicate_workflow,
    count_workflow_versions,
    version_run_count,
    delete_workflow_version,
    create_workflow_version,
    sanitize_graph_json,
    get_workflow_versions,
    get_workflow_version_by_id,
    get_or_create_project_workflows_folder,
    create_workflow_run_folder,
    create_workflow_step_folder,
    upgrade_workflow_node_pins,
)

from .team_cud import (
    create_team,
    get_teams,
    get_team,
    add_team_member
)

from .resource_lock_cud import (
    get_active_locks_for_tenant,
    get_active_lock_for_resource,
    acquire_lock,
    renew_lock,
    release_lock,
    force_release_lock,
    default_ttl_minutes,
)

from .webhook_cud import (
    create_webhook_endpoint,
    get_webhook_endpoints,
    get_webhook_endpoint,
    delete_webhook_endpoint
)

from .tag_cud import (
    create_tag,
    get_tags_for_tenant,
    get_tags_for_cog,
    add_tag_to_cog,
    remove_tag_from_cog
)

from .subscription_cud import (
    get_subscription_for_tenant,
    update_subscription_after_payment,
    update_subscription_status
)

from .node_definition_cud import (
    create_node_definition,
    get_node_definitions,
    get_node_definition_by_id,
    update_node_definition,
    delete_node_definition,
    soft_delete_node_definition,
    get_node_definition_by_name,
    promote_node_definition_to_tenant,
    record_node_version,
    resolve_node_run_pin,
    stamp_node_run_execution,
    current_node_version,
    resolve_pin_for_new_workflow_node,
    latest_versions_for_nodes,
)

from . import storage_connection_cud
from . import node_ownership_cud

from .file_cud import (
    create_file_record,
    get_file_record,
    create_workflow_run_file,
    get_workflow_run_files,
    get_workflow_run_file_by_id,
    update_workflow_run_file,
    delete_workflow_run_file
)

from .node_run_cud import (
    get_node_runs_for_workflow_run,
    get_node_run_by_id,
    update_node_run_status,
    get_workflow_runs,
    get_workflow_run_by_id
)

from .smtp_cud import (
    get_smtp_settings_by_user_id,
    create_smtp_settings,
    update_smtp_settings,
    delete_smtp_settings,
    get_email_config,
    create_or_update_email_config,
    get_decrypted_email_config,
    _ensure_secret_exists,
    _remove_secret_if_exists
)

from .role_cud import (
    create_custom_role,
    get_custom_roles,
    get_custom_role,
    update_custom_role,
    delete_custom_role
)

from .shareable_link_cud import (
    soft_delete_shareable_link,
    get_shareable_link_for_owner,
    create_shareable_link,
    get_shareable_links_for_cog,
    get_active_link_by_token,
    revoke_all_active_links,
)

# Remaining legacy imports (to preserve function availability during transition)
# These functions are defined in the main crud.py file and re-imported here
# to maintain backward compatibility until full migration is completed.
# === COG TAGS ===
# from crud.py - add_tag_to_cog, remove_tag_from_cog, get_tags_for_cog

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import models, schemas
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, desc, asc
import secrets
from logging import getLogger

# Re-export logger for backward compatibility
logger = getLogger(__name__)

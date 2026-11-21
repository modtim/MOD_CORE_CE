#!/usr/bin/env python3
"""Seed capabilities for the granular permission system - run from API container on startup"""

import sys
import os
import logging
import database
from database import SessionLocal
from models import Capability, ResourceType

# Setup logging
from utils.log_config import configure_logging
configure_logging()
logger = logging.getLogger(__name__)

# Check if debug mode is enabled (may expose sensitive data)
from services.debug_mode import is_debug_mode

# =============================================================================
# CAPABILITY DEFINITIONS
# =============================================================================
# Format: (resource_type, name, display_name, description, category, display_order, is_sensitive, requires)

CAPABILITIES = [
    # =========================================================================
    # TENANT CAPABILITIES
    # =========================================================================
    (ResourceType.TENANT, "view_settings", "View Settings", "View tenant configuration settings", "Settings", 10, False, []),
    (ResourceType.TENANT, "manage_settings", "Manage Settings", "Modify tenant configuration settings", "Settings", 20, True, ["view_settings"]),
    (ResourceType.TENANT, "view_billing", "View Account", "View account, notices, and usage", "Account", 30, False, []),
    (ResourceType.TENANT, "manage_billing", "Manage Account", "Change account settings and payment methods", "Account", 40, True, ["view_billing"]),
    (ResourceType.TENANT, "view_usage_analytics", "View Analytics", "View processing stats and storage usage", "Analytics", 50, False, []),
    (ResourceType.TENANT, "export_usage_analytics", "Export Analytics", "Export analytics data", "Analytics", 60, False, ["view_usage_analytics"]),
    (ResourceType.TENANT, "view_audit_logs", "View Audit Logs", "View security and compliance logs", "Compliance", 70, False, []),
    (ResourceType.TENANT, "export_audit_logs", "Export Audit Logs", "Export audit log data", "Compliance", 80, True, ["view_audit_logs"]),
    (ResourceType.TENANT, "manage_sso", "Manage SSO", "Configure SSO/SAML settings", "Security", 90, True, []),
    (ResourceType.TENANT, "manage_security_policies", "Manage Security Policies", "Configure password policies and session settings", "Security", 100, True, []),
    (ResourceType.TENANT, "manage_retention_policies", "Manage Retention", "Configure data retention rules", "Compliance", 110, True, []),
    (ResourceType.TENANT, "manage_compliance", "Manage Compliance", "Configure GDPR and compliance settings", "Compliance", 120, True, []),
    (ResourceType.TENANT, "impersonate_users", "Impersonate Users", "Login as another user for support", "Admin", 130, True, []),

    # =========================================================================
    # COG (FOLDER/FILE) CAPABILITIES
    # =========================================================================
    (ResourceType.COG, "view", "View", "See file/folder exists and view metadata", "Access", 10, False, []),
    (ResourceType.COG, "view_thumbnail", "View Thumbnails", "See preview thumbnails", "Access", 20, False, ["view"]),
    (ResourceType.COG, "preview", "Preview", "Preview files in browser without download", "Access", 30, False, ["view"]),
    (ResourceType.COG, "download_proxy", "Download Proxy", "Download low-resolution proxy versions", "Access", 40, False, ["view"]),
    (ResourceType.COG, "download_original", "Download Original", "Download original full-resolution files", "Access", 50, False, ["view"]),
    (ResourceType.COG, "download_bulk", "Bulk Download", "Download multiple files at once", "Access", 60, False, ["download_original"]),
    (ResourceType.COG, "upload", "Upload", "Upload new files", "Edit", 70, False, ["view"]),
    (ResourceType.COG, "upload_version", "Upload Version", "Upload new version of existing file", "Edit", 80, False, ["view"]),
    (ResourceType.COG, "create_subfolder", "Create Subfolder", "Create child folders", "Edit", 90, False, ["view"]),
    (ResourceType.COG, "rename", "Rename", "Rename file or folder", "Edit", 100, False, ["view"]),
    (ResourceType.COG, "move", "Move", "Move to different location", "Edit", 110, False, ["view"]),
    (ResourceType.COG, "copy", "Copy", "Copy/duplicate files or folders", "Edit", 120, False, ["view"]),
    (ResourceType.COG, "delete", "Delete", "Move to trash", "Delete", 130, False, ["view"]),
    (ResourceType.COG, "delete_permanently", "Delete Permanently", "Permanently delete (bypass trash)", "Delete", 140, True, ["delete"]),
    (ResourceType.COG, "restore", "Restore", "Restore from trash", "Delete", 150, False, ["view"]),
    (ResourceType.COG, "share", "Share", "Grant others access", "Sharing", 160, False, ["view"]),
    (ResourceType.COG, "manage_permissions", "Manage Permissions", "Edit or revoke existing permissions", "Sharing", 170, True, ["share"]),
    (ResourceType.COG, "view_permissions", "View Permissions", "See who has access", "Sharing", 180, False, ["view"]),
    (ResourceType.COG, "create_shareable_link", "Create Share Link", "Create public shareable links", "Sharing", 190, False, ["share"]),
    (ResourceType.COG, "comment", "Comment", "Add comments to files", "Collaboration", 200, False, ["view"]),
    (ResourceType.COG, "view_comments", "View Comments", "Read comments", "Collaboration", 210, False, ["view"]),
    (ResourceType.COG, "delete_comments", "Delete Comments", "Delete any comment", "Collaboration", 220, False, ["view_comments"]),
    (ResourceType.COG, "tag", "Tag", "Add or remove tags", "Metadata", 230, False, ["view"]),
    (ResourceType.COG, "edit_metadata", "Edit Metadata", "Edit custom metadata fields", "Metadata", 240, False, ["view"]),
    (ResourceType.COG, "view_versions", "View Versions", "See version history", "Versioning", 250, False, ["view"]),
    (ResourceType.COG, "restore_version", "Restore Version", "Restore a previous version", "Versioning", 260, False, ["view_versions"]),
    (ResourceType.COG, "delete_version", "Delete Version", "Delete specific versions", "Versioning", 270, True, ["view_versions"]),
    (ResourceType.COG, "lock", "Lock", "Lock file for exclusive editing", "Edit", 280, False, ["view"]),
    (ResourceType.COG, "unlock", "Unlock", "Unlock a locked file", "Edit", 290, False, ["view"]),
    (ResourceType.COG, "view_activity", "View Activity", "See activity log for this item", "Audit", 300, False, ["view"]),
    (ResourceType.COG, "process", "Process", "Run workflows on this item", "Processing", 310, False, ["view"]),
    (ResourceType.COG, "view_processing_history", "View Processing History", "See past workflow runs on this item", "Processing", 320, False, ["view"]),

    # =========================================================================
    # WORKFLOW CAPABILITIES
    # =========================================================================
    (ResourceType.WORKFLOW, "view", "View", "See workflow exists", "Access", 10, False, []),
    (ResourceType.WORKFLOW, "view_details", "View Details", "See workflow configuration and nodes", "Access", 20, False, ["view"]),
    (ResourceType.WORKFLOW, "execute", "Execute", "Run this workflow", "Execute", 30, False, ["view"]),
    (ResourceType.WORKFLOW, "execute_with_params", "Execute with Parameters", "Run with custom parameters", "Execute", 40, False, ["execute"]),
    (ResourceType.WORKFLOW, "create", "Create", "Create new workflows", "Edit", 50, False, []),  # Workflow Builder access
    (ResourceType.WORKFLOW, "edit", "Edit", "Modify workflow configuration", "Edit", 60, False, ["view"]),
    (ResourceType.WORKFLOW, "delete", "Delete", "Delete workflow", "Delete", 70, True, ["view"]),
    (ResourceType.WORKFLOW, "duplicate", "Duplicate", "Clone workflow", "Edit", 80, False, ["view"]),
    (ResourceType.WORKFLOW, "share", "Share", "Grant others access", "Sharing", 90, False, ["view"]),
    (ResourceType.WORKFLOW, "manage_permissions", "Manage Permissions", "Edit workflow permissions", "Sharing", 100, True, ["share"]),
    (ResourceType.WORKFLOW, "view_permissions", "View Permissions", "See who can access", "Sharing", 110, False, ["view"]),
    (ResourceType.WORKFLOW, "publish", "Publish", "Mark as ready for production", "Lifecycle", 120, False, ["edit"]),
    (ResourceType.WORKFLOW, "unpublish", "Unpublish", "Remove from production", "Lifecycle", 130, False, ["edit"]),
    (ResourceType.WORKFLOW, "view_versions", "View Versions", "See version history", "Versioning", 140, False, ["view"]),
    (ResourceType.WORKFLOW, "restore_version", "Restore Version", "Restore a previous version", "Versioning", 150, False, ["view_versions", "edit"]),
    (ResourceType.WORKFLOW, "create_trigger", "Create Trigger", "Set up automated triggers", "Automation", 160, False, ["view"]),
    (ResourceType.WORKFLOW, "manage_triggers", "Manage Triggers", "Edit or delete triggers", "Automation", 170, False, ["create_trigger"]),
    (ResourceType.WORKFLOW, "view_triggers", "View Triggers", "See existing triggers", "Automation", 180, False, ["view"]),
    (ResourceType.WORKFLOW, "view_runs", "View Runs", "See execution history", "Monitoring", 190, False, ["view"]),
    (ResourceType.WORKFLOW, "export", "Export", "Export workflow definition", "Transfer", 200, False, ["view"]),
    (ResourceType.WORKFLOW, "import", "Import", "Import workflow definition", "Transfer", 210, False, ["create"]),

    # =========================================================================
    # WORKFLOW RUN CAPABILITIES
    # =========================================================================
    (ResourceType.WORKFLOW_RUN, "view", "View", "See run exists", "Access", 10, False, []),
    (ResourceType.WORKFLOW_RUN, "view_status", "View Status", "See running/completed/failed status", "Monitoring", 20, False, ["view"]),
    (ResourceType.WORKFLOW_RUN, "view_progress", "View Progress", "See detailed progress", "Monitoring", 30, False, ["view_status"]),
    (ResourceType.WORKFLOW_RUN, "view_logs", "View Logs", "Read execution logs", "Monitoring", 40, False, ["view"]),
    (ResourceType.WORKFLOW_RUN, "download_logs", "Download Logs", "Export logs to file", "Monitoring", 50, False, ["view_logs"]),
    (ResourceType.WORKFLOW_RUN, "view_outputs", "View Outputs", "See output files", "Outputs", 60, False, ["view"]),
    (ResourceType.WORKFLOW_RUN, "download_outputs", "Download Outputs", "Download output files", "Outputs", 70, False, ["view_outputs"]),
    (ResourceType.WORKFLOW_RUN, "cancel", "Cancel", "Stop running workflow", "Control", 80, False, ["view"]),
    (ResourceType.WORKFLOW_RUN, "retry", "Retry", "Re-run failed workflow", "Control", 90, False, ["view"]),
    (ResourceType.WORKFLOW_RUN, "retry_from_step", "Retry from Step", "Retry from specific step", "Control", 100, False, ["retry"]),
    (ResourceType.WORKFLOW_RUN, "delete", "Delete", "Delete run history", "Delete", 110, True, ["view"]),
    (ResourceType.WORKFLOW_RUN, "view_cost", "View Cost", "See credit cost", "Usage", 120, False, ["view"]),
    (ResourceType.WORKFLOW_RUN, "view_parameters", "View Parameters", "See input parameters", "Details", 130, False, ["view"]),

    # =========================================================================
    # NODE DEFINITION CAPABILITIES
    # =========================================================================
    (ResourceType.NODE_DEFINITION, "view", "View", "See node exists", "Access", 10, False, []),
    (ResourceType.NODE_DEFINITION, "use", "Use", "Use in workflows", "Use", 20, False, ["view"]),
    (ResourceType.NODE_DEFINITION, "create", "Create", "Create custom nodes", "Edit", 30, False, []),
    (ResourceType.NODE_DEFINITION, "edit", "Edit", "Modify node definition", "Edit", 40, False, ["view"]),
    (ResourceType.NODE_DEFINITION, "delete", "Delete", "Delete node", "Delete", 50, True, ["view"]),
    (ResourceType.NODE_DEFINITION, "view_cost", "View Cost", "See credit cost", "Usage", 60, False, ["view"]),
    (ResourceType.NODE_DEFINITION, "publish", "Publish", "Make available to others", "Lifecycle", 70, False, ["edit"]),

    # =========================================================================
    # USER CAPABILITIES
    # =========================================================================
    (ResourceType.USER, "view", "View", "See user exists", "Access", 10, False, []),
    (ResourceType.USER, "view_details", "View Details", "See email, name, and role", "Access", 20, False, ["view"]),
    (ResourceType.USER, "view_activity", "View Activity", "See user's activity history", "Monitoring", 30, False, ["view"]),
    (ResourceType.USER, "invite", "Invite", "Send invitations to new users", "Management", 40, False, []),
    (ResourceType.USER, "edit_profile", "Edit Profile", "Edit user profile information", "Management", 50, False, ["view"]),
    (ResourceType.USER, "change_role", "Change Role", "Assign different role to user", "Management", 60, True, ["view"]),
    (ResourceType.USER, "deactivate", "Deactivate", "Disable user account", "Management", 70, True, ["view"]),
    (ResourceType.USER, "reactivate", "Reactivate", "Re-enable user account", "Management", 80, True, ["view"]),
    (ResourceType.USER, "delete", "Delete", "Permanently delete user", "Delete", 90, True, ["view"]),
    (ResourceType.USER, "reset_password", "Reset Password", "Force password reset", "Security", 100, True, ["view"]),
    (ResourceType.USER, "manage_sessions", "Manage Sessions", "View and revoke active sessions", "Security", 110, True, ["view"]),
    (ResourceType.USER, "view_permissions", "View Permissions", "See user's permissions", "Access", 120, False, ["view"]),
    (ResourceType.USER, "manage_permissions", "Manage Permissions", "Grant or revoke direct permissions", "Management", 130, True, ["view_permissions"]),

    # =========================================================================
    # TEAM CAPABILITIES
    # =========================================================================
    (ResourceType.TEAM, "view", "View", "See team exists", "Access", 10, False, []),
    (ResourceType.TEAM, "view_members", "View Members", "See team membership", "Access", 20, False, ["view"]),
    (ResourceType.TEAM, "create", "Create", "Create new teams", "Management", 30, False, []),
    (ResourceType.TEAM, "edit", "Edit", "Rename or change description", "Management", 40, False, ["view"]),
    (ResourceType.TEAM, "delete", "Delete", "Delete team", "Delete", 50, True, ["view"]),
    (ResourceType.TEAM, "add_members", "Add Members", "Add users to team", "Membership", 60, False, ["view_members"]),
    (ResourceType.TEAM, "remove_members", "Remove Members", "Remove users from team", "Membership", 70, False, ["view_members"]),
    (ResourceType.TEAM, "manage_permissions", "Manage Permissions", "Grant permissions to team", "Permissions", 80, True, ["view"]),

    # =========================================================================
    # ROLE CAPABILITIES
    # =========================================================================
    (ResourceType.ROLE, "view", "View", "See role exists", "Access", 10, False, []),
    (ResourceType.ROLE, "view_details", "View Details", "See role capabilities", "Access", 20, False, ["view"]),
    (ResourceType.ROLE, "create", "Create", "Create new roles", "Management", 30, False, []),
    (ResourceType.ROLE, "edit", "Edit", "Modify role capabilities", "Management", 40, True, ["view"]),
    (ResourceType.ROLE, "delete", "Delete", "Delete role", "Delete", 50, True, ["view"]),
    (ResourceType.ROLE, "assign", "Assign", "Assign role to users", "Assignment", 60, True, ["view"]),
    (ResourceType.ROLE, "unassign", "Unassign", "Remove role from users", "Assignment", 70, True, ["view"]),

    # =========================================================================
    # API KEY CAPABILITIES
    # =========================================================================
    (ResourceType.API_KEY, "view", "View", "See API key exists (not the secret)", "Access", 10, False, []),
    (ResourceType.API_KEY, "create", "Create", "Generate new API keys", "Management", 20, False, []),
    (ResourceType.API_KEY, "edit", "Edit", "Rename or change permissions", "Management", 30, False, ["view"]),
    (ResourceType.API_KEY, "delete", "Delete", "Revoke API key", "Delete", 40, True, ["view"]),
    (ResourceType.API_KEY, "view_usage", "View Usage", "See API key usage stats", "Monitoring", 50, False, ["view"]),

    # =========================================================================
    # SECRET CAPABILITIES
    # =========================================================================
    (ResourceType.SECRET, "view", "View", "See secret exists (not the value)", "Access", 10, False, []),
    (ResourceType.SECRET, "view_value", "View Value", "See actual secret value", "Access", 20, True, ["view"]),
    (ResourceType.SECRET, "create", "Create", "Create new secrets", "Management", 30, False, []),
    (ResourceType.SECRET, "edit", "Edit", "Update secret value", "Management", 40, True, ["view"]),
    (ResourceType.SECRET, "delete", "Delete", "Delete secret", "Delete", 50, True, ["view"]),
    (ResourceType.SECRET, "use_in_workflow", "Use in Workflow", "Reference secret in workflows", "Use", 60, False, ["view"]),

    # =========================================================================
    # INTEGRATION CAPABILITIES
    # =========================================================================
    (ResourceType.INTEGRATION, "view", "View", "See integration exists", "Access", 10, False, []),
    (ResourceType.INTEGRATION, "view_config", "View Config", "See configuration details", "Access", 20, False, ["view"]),
    (ResourceType.INTEGRATION, "create", "Create", "Set up new integrations", "Management", 30, False, []),
    (ResourceType.INTEGRATION, "edit", "Edit", "Modify configuration", "Management", 40, True, ["view"]),
    (ResourceType.INTEGRATION, "delete", "Delete", "Remove integration", "Delete", 50, True, ["view"]),
    (ResourceType.INTEGRATION, "test", "Test", "Test connection", "Operations", 60, False, ["view"]),
    (ResourceType.INTEGRATION, "sync", "Sync", "Trigger manual sync", "Operations", 70, False, ["view"]),
    (ResourceType.INTEGRATION, "view_logs", "View Logs", "See sync logs", "Monitoring", 80, False, ["view"]),

    # =========================================================================
    # WORKER CAPABILITIES
    # =========================================================================
    (ResourceType.WORKER, "view", "View", "See worker exists", "Access", 10, False, []),
    (ResourceType.WORKER, "view_status", "View Status", "See online/offline and capacity", "Monitoring", 20, False, ["view"]),
    (ResourceType.WORKER, "view_logs", "View Logs", "See worker logs", "Monitoring", 30, False, ["view"]),
    (ResourceType.WORKER, "start", "Start", "Start worker", "Control", 40, True, ["view"]),
    (ResourceType.WORKER, "stop", "Stop", "Stop worker", "Control", 50, True, ["view"]),
    (ResourceType.WORKER, "restart", "Restart", "Restart worker", "Control", 60, True, ["view"]),
    (ResourceType.WORKER, "configure", "Configure", "Change worker settings", "Management", 70, True, ["view"]),
    (ResourceType.WORKER, "delete", "Delete", "Remove worker", "Delete", 80, True, ["view"]),
    (ResourceType.WORKER, "view_jobs", "View Jobs", "See jobs on this worker", "Monitoring", 90, False, ["view"]),
    (ResourceType.WORKER, "cancel_jobs", "Cancel Jobs", "Cancel jobs on this worker", "Control", 100, True, ["view_jobs"]),

    # =========================================================================
    # SHAREABLE LINK CAPABILITIES
    # =========================================================================
    (ResourceType.SHAREABLE_LINK, "view", "View", "See links exist", "Access", 10, False, []),
    (ResourceType.SHAREABLE_LINK, "create", "Create", "Create new share links", "Management", 20, False, []),
    (ResourceType.SHAREABLE_LINK, "edit", "Edit", "Change expiration or password", "Management", 30, False, ["view"]),
    (ResourceType.SHAREABLE_LINK, "delete", "Delete", "Revoke link", "Delete", 40, False, ["view"]),
    (ResourceType.SHAREABLE_LINK, "view_analytics", "View Analytics", "See download counts", "Monitoring", 50, False, ["view"]),

    # =========================================================================
    # COMMENT CAPABILITIES
    # =========================================================================
    (ResourceType.COMMENT, "create", "Create", "Add comments", "Creation", 10, False, []),
    (ResourceType.COMMENT, "edit_own", "Edit Own", "Edit your comments", "Edit", 20, False, []),
    (ResourceType.COMMENT, "edit_any", "Edit Any", "Edit any comment", "Edit", 30, False, []),
    (ResourceType.COMMENT, "delete_own", "Delete Own", "Delete your comments", "Delete", 40, False, []),
    (ResourceType.COMMENT, "delete_any", "Delete Any", "Delete any comment", "Delete", 50, True, []),
    (ResourceType.COMMENT, "resolve", "Resolve", "Mark as resolved", "Moderation", 60, False, []),
    (ResourceType.COMMENT, "mention", "Mention", "@mention users in comments", "Collaboration", 70, False, ["create"]),

    # =========================================================================
    # TAG CAPABILITIES
    # =========================================================================
    (ResourceType.TAG, "view", "View", "See tags", "Access", 10, False, []),
    (ResourceType.TAG, "create", "Create", "Create new tags", "Management", 20, False, []),
    (ResourceType.TAG, "edit", "Edit", "Rename tags", "Management", 30, False, ["view"]),
    (ResourceType.TAG, "delete", "Delete", "Delete tags", "Delete", 40, True, ["view"]),
    (ResourceType.TAG, "assign", "Assign", "Apply tags to items", "Use", 50, False, ["view"]),
    (ResourceType.TAG, "unassign", "Unassign", "Remove tags from items", "Use", 60, False, ["view"]),

    # =========================================================================
    # WEBHOOK CAPABILITIES
    # =========================================================================
    (ResourceType.WEBHOOK, "view", "View", "See webhooks exist", "Access", 10, False, []),
    (ResourceType.WEBHOOK, "create", "Create", "Create webhooks", "Management", 20, False, []),
    (ResourceType.WEBHOOK, "edit", "Edit", "Modify webhook config", "Management", 30, False, ["view"]),
    (ResourceType.WEBHOOK, "delete", "Delete", "Delete webhooks", "Delete", 40, True, ["view"]),
    (ResourceType.WEBHOOK, "view_logs", "View Logs", "See delivery logs", "Monitoring", 50, False, ["view"]),
    (ResourceType.WEBHOOK, "retry", "Retry", "Retry failed deliveries", "Operations", 60, False, ["view_logs"]),

    # =========================================================================
    # SCHEDULE CAPABILITIES
    # =========================================================================
    (ResourceType.SCHEDULE, "view", "View", "See scheduled jobs", "Access", 10, False, []),
    (ResourceType.SCHEDULE, "create", "Create", "Create schedules", "Management", 20, False, []),
    (ResourceType.SCHEDULE, "edit", "Edit", "Modify schedules", "Management", 30, False, ["view"]),
    (ResourceType.SCHEDULE, "delete", "Delete", "Delete schedules", "Delete", 40, True, ["view"]),
    (ResourceType.SCHEDULE, "enable", "Enable", "Enable schedule", "Control", 50, False, ["view"]),
    (ResourceType.SCHEDULE, "disable", "Disable", "Disable schedule", "Control", 60, False, ["view"]),
    (ResourceType.SCHEDULE, "run_now", "Run Now", "Trigger immediate run", "Control", 70, False, ["view"]),

    # =========================================================================
    # REPORT CAPABILITIES
    # =========================================================================
    (ResourceType.REPORT, "view", "View", "See reports", "Access", 10, False, []),
    (ResourceType.REPORT, "create", "Create", "Generate reports", "Creation", 20, False, []),
    (ResourceType.REPORT, "download", "Download", "Download reports", "Access", 30, False, ["view"]),
    (ResourceType.REPORT, "delete", "Delete", "Delete reports", "Delete", 40, False, ["view"]),
    (ResourceType.REPORT, "schedule", "Schedule", "Set up recurring reports", "Automation", 50, False, ["create"]),
    (ResourceType.REPORT, "share", "Share", "Share reports with others", "Sharing", 60, False, ["view"]),

    # =========================================================================
    # NOTIFICATION CAPABILITIES
    # =========================================================================
    (ResourceType.NOTIFICATION, "view_own", "View Own", "See your notifications", "Access", 10, False, []),
    (ResourceType.NOTIFICATION, "manage_own_preferences", "Manage Own Preferences", "Change your notification settings", "Preferences", 20, False, []),
    (ResourceType.NOTIFICATION, "manage_all_preferences", "Manage All Preferences", "Change notification settings for others", "Admin", 30, True, []),
    (ResourceType.NOTIFICATION, "send_announcement", "Send Announcement", "Send tenant-wide announcements", "Admin", 40, True, []),

    # =========================================================================
    # STORAGE LOCATION CAPABILITIES
    # =========================================================================
    (ResourceType.STORAGE_LOCATION, "view", "View", "See storage backends", "Access", 10, False, []),
    (ResourceType.STORAGE_LOCATION, "create", "Create", "Add new storage location", "Management", 20, True, []),
    (ResourceType.STORAGE_LOCATION, "edit", "Edit", "Modify configuration", "Management", 30, True, ["view"]),
    (ResourceType.STORAGE_LOCATION, "delete", "Delete", "Remove storage location", "Delete", 40, True, ["view"]),
    (ResourceType.STORAGE_LOCATION, "set_default", "Set Default", "Set as default storage", "Management", 50, True, ["view"]),
    (ResourceType.STORAGE_LOCATION, "view_usage", "View Usage", "See storage usage stats", "Monitoring", 60, False, ["view"]),
    (ResourceType.STORAGE_LOCATION, "migrate", "Migrate", "Move data between locations", "Operations", 70, True, ["view"]),

    # =========================================================================
    # PERMISSION TEMPLATE CAPABILITIES
    # =========================================================================
    (ResourceType.PERMISSION_TEMPLATE, "view", "View", "See permission templates", "Access", 10, False, []),
    (ResourceType.PERMISSION_TEMPLATE, "create", "Create", "Create new templates", "Management", 20, False, []),
    (ResourceType.PERMISSION_TEMPLATE, "edit", "Edit", "Modify templates", "Management", 30, False, ["view"]),
    (ResourceType.PERMISSION_TEMPLATE, "delete", "Delete", "Delete templates", "Delete", 40, False, ["view"]),
    (ResourceType.PERMISSION_TEMPLATE, "apply", "Apply", "Apply templates to grant permissions", "Use", 50, False, ["view"]),
    (ResourceType.PERMISSION_TEMPLATE, "duplicate", "Duplicate", "Clone templates", "Management", 60, False, ["view"]),

    # =========================================================================
    # INVITATION CAPABILITIES
    # =========================================================================
    (ResourceType.INVITATION, "view", "View", "See pending invitations", "Access", 10, False, []),
    (ResourceType.INVITATION, "create", "Create", "Send invitations", "Management", 20, False, []),
    (ResourceType.INVITATION, "cancel", "Cancel", "Cancel pending invitations", "Management", 30, False, ["view"]),
    (ResourceType.INVITATION, "resend", "Resend", "Resend invitation email", "Management", 40, False, ["view"]),

    # =========================================================================
    # SUBSCRIPTION CAPABILITIES
    # =========================================================================
    (ResourceType.SUBSCRIPTION, "view", "View", "View account details", "Access", 10, False, []),
    (ResourceType.SUBSCRIPTION, "change_plan", "Change Account", "Change account settings", "Management", 20, True, ["view"]),
    (ResourceType.SUBSCRIPTION, "cancel", "Cancel", "Cancel account", "Management", 30, True, ["view"]),
    (ResourceType.SUBSCRIPTION, "view_invoices", "View Notices", "See account history", "Account", 40, False, ["view"]),
    (ResourceType.SUBSCRIPTION, "download_invoices", "Download Notices", "Download notice PDFs", "Account", 50, False, ["view_invoices"]),

    # =========================================================================
    # AUDIT LOG CAPABILITIES
    # =========================================================================
    (ResourceType.AUDIT_LOG, "view", "View", "View audit logs", "Access", 10, False, []),
    (ResourceType.AUDIT_LOG, "export", "Export", "Export audit logs", "Export", 20, True, ["view"]),
    (ResourceType.AUDIT_LOG, "filter_by_user", "Filter by User", "Filter logs by specific user", "Access", 30, False, ["view"]),
    (ResourceType.AUDIT_LOG, "filter_by_resource", "Filter by Resource", "Filter logs by resource", "Access", 40, False, ["view"]),

    # =========================================================================
    # VERSION CAPABILITIES
    # =========================================================================
    (ResourceType.VERSION, "view", "View", "View version history", "Access", 10, False, []),
    (ResourceType.VERSION, "compare", "Compare", "Compare versions", "Access", 20, False, ["view"]),
    (ResourceType.VERSION, "restore", "Restore", "Restore to a version", "Management", 30, False, ["view"]),
    (ResourceType.VERSION, "delete", "Delete", "Delete specific versions", "Delete", 40, True, ["view"]),
]


def seed_all_capabilities(db):
    """
    Seed all capabilities to the database.

    This function is called from main.py on startup.

    Args:
        db: SQLAlchemy session

    Returns:
        tuple: (created_count, updated_count)
    """
    # Get existing capability names by resource type
    existing_caps = {}
    for cap in db.query(Capability).all():
        key = (cap.resource_type, cap.name)
        existing_caps[key] = cap

    # Track stats
    created = 0
    updated = 0

    # Process each capability
    for cap_data in CAPABILITIES:
        resource_type, name, display_name, description, category, display_order, is_sensitive, requires = cap_data

        key = (resource_type, name)

        if key in existing_caps:
            # Update existing capability if needed
            existing = existing_caps[key]
            needs_update = False

            if existing.display_name != display_name:
                existing.display_name = display_name
                needs_update = True
            if existing.description != description:
                existing.description = description
                needs_update = True
            if existing.category != category:
                existing.category = category
                needs_update = True
            if existing.display_order != display_order:
                existing.display_order = display_order
                needs_update = True
            if existing.is_sensitive != is_sensitive:
                existing.is_sensitive = is_sensitive
                needs_update = True
            if existing.requires_capabilities != requires:
                existing.requires_capabilities = requires
                needs_update = True

            if needs_update:
                updated += 1
        else:
            # Create new capability
            cap = Capability(
                resource_type=resource_type,
                name=name,
                display_name=display_name,
                description=description,
                category=category,
                display_order=display_order,
                is_sensitive=is_sensitive,
                requires_capabilities=requires
            )
            db.add(cap)
            created += 1

    db.commit()
    return created, updated


def main():
    database.initialize()
    logger.info("=" * 60)
    logger.info("Seeding Capabilities for Granular Permission System")
    logger.info("=" * 60)
    logger.info("")

    db = SessionLocal()

    try:
        # Check existing capabilities
        existing_count = db.query(Capability).count()

        if existing_count > 0:
            logger.info(f"Found {existing_count} existing capabilities")
            logger.info("Checking for new capabilities to add...")
            logger.info("")

        # Get existing capability names by resource type
        existing_caps = {}
        for cap in db.query(Capability).all():
            key = (cap.resource_type, cap.name)
            existing_caps[key] = cap

        # Track stats
        created = 0
        updated = 0
        skipped = 0

        # Process each capability
        for cap_data in CAPABILITIES:
            resource_type, name, display_name, description, category, display_order, is_sensitive, requires = cap_data

            key = (resource_type, name)

            if key in existing_caps:
                # Update existing capability if needed
                existing = existing_caps[key]
                needs_update = False

                if existing.display_name != display_name:
                    existing.display_name = display_name
                    needs_update = True
                if existing.description != description:
                    existing.description = description
                    needs_update = True
                if existing.category != category:
                    existing.category = category
                    needs_update = True
                if existing.display_order != display_order:
                    existing.display_order = display_order
                    needs_update = True
                if existing.is_sensitive != is_sensitive:
                    existing.is_sensitive = is_sensitive
                    needs_update = True
                if existing.requires_capabilities != requires:
                    existing.requires_capabilities = requires
                    needs_update = True

                if needs_update:
                    updated += 1
                else:
                    skipped += 1
            else:
                # Create new capability
                cap = Capability(
                    resource_type=resource_type,
                    name=name,
                    display_name=display_name,
                    description=description,
                    category=category,
                    display_order=display_order,
                    is_sensitive=is_sensitive,
                    requires_capabilities=requires
                )
                db.add(cap)
                created += 1

        db.commit()

        # Print summary by resource type
        logger.info("Capability Summary by Resource Type:")
        logger.info("-" * 40)

        resource_counts = {}
        for cap_data in CAPABILITIES:
            rt = cap_data[0].value
            resource_counts[rt] = resource_counts.get(rt, 0) + 1

        for rt, count in sorted(resource_counts.items()):
            logger.info(f"  {rt:<25} {count:>3} capabilities")

        logger.info("-" * 40)
        logger.info(f"  {'TOTAL':<25} {len(CAPABILITIES):>3} capabilities")
        logger.info("")

        logger.info(f"Results:")
        logger.info(f"  Created: {created}")
        logger.info(f"  Updated: {updated}")
        logger.info(f"  Skipped (unchanged): {skipped}")
        logger.info("")
        logger.info("=" * 60)

        return True

    except Exception as e:
        if is_debug_mode():
            logger.error(f"Error seeding capabilities: {e}")
            import traceback
            traceback.print_exc()
        else:
            logger.error(f"Error seeding capabilities: {type(e).__name__}")
        db.rollback()
        return False
    finally:
        db.close()


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

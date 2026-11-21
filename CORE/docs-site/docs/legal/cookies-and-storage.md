---
sidebar_position: 11
slug: /legal/cookies-and-storage
title: Cookies and Client-Side Storage
description: The cookies and browser storage used by the MOD Dashboard and WorkflowBuilder, what each is for, and how to clear them.
---

# Cookies and Client-Side Storage

**Last updated: November 2026**

The Dashboard and WorkflowBuilder use a small number of cookies and browser storage to manage your session and remember your preferences. We do not use tracking cookies, advertising cookies, third-party analytics, or third-party error telemetry. Our automated test suite includes a static scan that flags analytics or advertising trackers added to the frontend without a consent banner.

Lifetimes: a session cookie lasts until you close your browser; sessionStorage lasts until you close the tab; localStorage persists until you clear it.

## Cookies

| Name | Purpose | Lifetime | Category |
|---|---|---|---|
| `mod_active_identity` | The Keycloak user id (sub, a UUID) of the active signed-in identity, used to stop one app resurrecting another user's session. It is an identifier, not a credential. | Session (until browser close) | Strictly necessary (session management) |
| `mod_theme` | Theme preference, shared between Dashboard and WorkflowBuilder | Up to 1 year | Preference |
| `mod.assistant.pending` | Assistant-panel navigation handoff between the two apps | 60 seconds | Preference |
| `mod_assistant_open` | Assistant panel open/closed state across apps | Up to 1 year | Preference |
| `workflowNodesLoaded` | Flag that the node library has been loaded | 30 days | Preference |
| `sidebar:state` | Sidebar open/closed preference in the WorkflowBuilder | 7 days | Preference |

## Browser storage (localStorage — persists until cleared)

| Key | Purpose | Category |
|---|---|---|
| `post_login_hash` | Post-login integrity check | Strictly necessary (auth flow) |
| `pkce_code_verifier` | OIDC PKCE verifier for the login flow | Strictly necessary (auth flow) |
| `mod_pending_action` | Action to resume after sign-in | Strictly necessary (auth flow) |
| `upload_<filename>_<size>` | File-upload resume state | Preference |
| `changelog_last_seen` | "What's new" read marker | Preference |
| `help.activeTab` | Help page tab preference | Preference |
| `fileViewMode` | File list view preference | Preference |
| `checklist_celebrated_steps` | Onboarding checklist state | Preference |
| `mod.dashNotifFilter.v1` / `mod.notifBellFilter.v1` | Notification filter preferences | Preference |
| `mod_ai_master_mode` | AI master-mode UI preference | Preference |
| `mod_theme` | Theme preference (mirror of the cookie) | Preference |
| `mod.assistantPanelOpen` / `mod.assistantPanelWidth` | Assistant panel open state and width | Preference |
| `wb_node_library_width` | Node library sidebar width | Preference |
| `wfbViewMode` | File browser view preference | Preference |
| `mod.newNodeWizard.state.v1` / `mod.wizard.autoMode` | Node wizard draft state and mode preference | Preference |
| Build-queue key (per user) | Per-user node-build queue state | Preference |
| `mod.workflowSave.suppressSanitization.<workflowId>` | Per-workflow save-sanitization suppression snapshot | Preference |
| Mobile-enrollment key | Last mobile-enrollment run id | Preference |

## Browser storage (sessionStorage — cleared when the tab closes)

| Key | Purpose | Category |
|---|---|---|
| `accessToken` / `refreshToken` / `idToken` | Keycloak auth tokens (Dashboard) | Strictly necessary (session management) |
| `workflow_access_token` / `workflow_refresh_token` | Keycloak tokens for the WorkflowBuilder, held for this tab only (sessionStorage, not localStorage) | Strictly necessary (session management) |
| `verify_email_auto_sent` | Email-verification flow state | Strictly necessary (auth flow) |
| `pending_invitation_token` | Invitation acceptance flow state | Strictly necessary (auth flow) |
| `mod.assistantSessionId` | Assistant conversation session id | Preference |
| `help.mindmap.tree.v1` | Help mindmap cache | Preference |
| `perm-active-tab` / `perm-expanded-folders` / `perm-selected-cog-id` | Permissions page UI state | Preference |
| `mod_scheduler_training_run_v1` | Scheduler-training run UI state | Preference |
| `mod_store_pending_checkout` | Pending store checkout state (payment flow) | Preference |

## Managing these

You can clear cookies and browser storage at any time from your browser's settings. Doing so signs you out of the affected apps and resets the listed preferences; it does not delete anything from our servers.

## Contact

Data Protection Officer: admin@modtechlabs.com

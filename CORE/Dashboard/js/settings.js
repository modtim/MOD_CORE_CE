// settings.js
// Handles the unified Settings page functionality

import { notifications, secrets, apiKeys, me } from './api.js';
import { loadModal, closeModal } from './ui.js';
import { fetchApi as apiCall, isPaidEdition, getPlatformFeatures } from './api.js';
import { loadResourcePermissions } from './resource-permissions.js';
import { SMTP_PRESETS, inferSmtpPreset, smtpPresetFields, smtpProviderForSave } from './smtp-presets.js';
// TOKMERGE J5 — local escape for server strings interpolated into the key
// list (stack names); mirrors the escapeHtml helper every other page file
// carries, kept local so this file needs no new import.
function _skEsc(v) {
    return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// GE.AIC.6 (2026-09-29): embedding-models.js's initializeEmbeddingModelsPage
// import removed along with the Models tab that called it — the module
// itself, its em-* backing endpoints, and any already-uploaded models are
// untouched.
// tenant-assistant-knowledge-base: side-effect import — the module self-wires
// the KB section via a MutationObserver (survives the SPA innerHTML page swaps).
import './assistant-knowledge.js';
import './assistant-model-picker.js';
import { initAiConfigViewToggle } from './ai-config-view-toggle.js';
// mod-assistant-agent §6.4 (Dashboard half): shared action runtime — settings.js must
// not carry its own action vocabulary. Side-effect import; the module publishes
// window.ModAssistantActions.
import './shared/assistant-actions.js';
import { initializeMobileEnrollmentPage } from './mobile-enrollment.js';
import { initializeMobileDevicesPanel } from './mobile-devices.js';
// BUG.B36c — no initializeWebhooksPage import: the Integrations → Outbound
// Webhooks sub-tab is retired (B36a/B36b put every webhook on the ONE Apps-tab
// "Webhooks" section, System → Apps → Webhooks). js/webhooks.js stays alive as
// js/apps.js imports its listEndpoints/createEndpoint/removeEndpoint helpers.
import { initializeStorageConnectionsPage } from './storage-connections.js';
import { initializeSettingsOauthConnections } from './settings-oauth-connections.js';
import { initializeK8sClustersPage } from './k8s-clusters.js';
import { initBudgetsPanel } from './settings-budgets.js';
import { setActiveTab, getActiveTab } from './tab-state.js';
import {
    loadSchedulerRuntimeConfig,
    saveSchedulerRuntimeConfig,
    generateTrainingData as generateTrainingDataAction,
    pollTrainingProgress,
    detectScheduleRuntimePreset,
    getSchedulerPresetValues,
} from './scheduler-actions.js';

let currentUser = null;
let isSuperAdmin = false;

export function initializeSettingsPage() {
    // ui.js re-injects settings.html on every visit while this module stays
    // cached, so the "wired once" flags below must reset per mount — otherwise
    // the second visit's fresh forms get no handlers at all.
    _masterBarWired = false;
    _schedulerSubTabWired = false;
    _trainingSubTabWired = false;
    _wizardLearningWired = false;
    _wizardKnowledgePackWired = false;
    _resourceMonitorSubTabWired = false;
    _agentSubTabWired = false;
    _byokLoaded = false;
    // If the previous mount's save is somehow still out, the new mount's
    // refresh must be free to paint — a stale "in flight" flag would leave
    // the fresh form unhydrated forever. (seq stays monotonic: a counter
    // that only ever grows can never accidentally revalidate a stale GET.)
    _agentSaveInFlight = false;
    // Build stamp. Several rounds of "this is still broken" turned out to be a
    // page loaded before the rebuild, and there was no way to tell from the
    // screen. One line in the console answers it: if this timestamp predates
    // the fix you are testing, hard-refresh before reporting anything.
    console.log('Settings page build: 2026-08-07T23:00:43Z');
    console.log('Initializing unified Settings page');

    // Verify user has access to settings
    verifyAccess();

    // Main tab switching
    document.querySelectorAll('.main-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            // Update tab styles
            document.querySelectorAll('.main-tab').forEach(t => {
                t.classList.remove('border-primary', 'text-primary');
                t.classList.add('border-transparent', 'text-gray-500');
                t.setAttribute('aria-selected', 'false');
            });
            tab.classList.add('border-primary', 'text-primary');
            tab.classList.remove('border-transparent', 'text-gray-500');
            tab.setAttribute('aria-selected', 'true');

            // Remember the active sub-tab so a refresh restores it (#20).
            setActiveTab(tab.id.replace('main-tab-', ''));

            // Switch panels
            const panelId = tab.id.replace('main-tab-', 'main-panel-');
            document.querySelectorAll('.main-panel').forEach(p => p.classList.add('hidden'));
            document.getElementById(panelId).classList.remove('hidden');

            // Initialize tab content if needed
            if (panelId === 'main-panel-secrets') {
                loadSecrets();
            } else if (panelId === 'main-panel-apikeys') {
                loadApiKeys();
            } else if (panelId === 'main-panel-ai') {
                loadAIConfig();
            } else if (panelId === 'main-panel-integrations') {
                loadIntegrations();
            } else if (panelId === 'main-panel-storage') {
                loadStorage();
                // Storage Connections (BYO-S3) merged into this tab. Bind once
                // per page mount — its handlers use addEventListener, so the
                // dataset guard avoids double-binding on repeat tab clicks; the
                // guard resets when the Settings DOM is rebuilt on re-navigation.
                const _sp = document.getElementById('main-panel-storage');
                if (_sp && !_sp.dataset.connInit) {
                    _sp.dataset.connInit = '1';
                    initializeStorageConnectionsPage();
                }
            } else if (panelId === 'main-panel-permissions') {
                loadPermissionSettings();
            } else if (panelId === 'main-panel-tls') {
                loadStackTls();
            } else if (panelId === 'main-panel-byok') {
                _openByokTab();
            } else if (panelId === 'main-panel-knowledge') {
                // BUG.B59c (owner Q-B59=A) — the knowledge-sources picker now lives
                // on the Knowledge Base tab, so it hydrates on THIS click. Fire-and-
                // forget: _loadAiKnowledgeSources handles its own failure notice
                // (_aiKbNotice), and _wireAiKnowledgeSources is idempotent
                // (box.dataset.aiKbWired). assistant-knowledge.js still self-mounts
                // the rest of this panel; the loadAIConfig() call sites stay for
                // re-entry (they hydrate the box when the AI Config strip repaints).
                _loadAiKnowledgeSources();
                _wireAiKnowledgeSources();
            }
        });
    });

    // AI configuration wiring happens inside loadAIConfig() once sub-panels
    // are rendered from the <template> element.

    // Integrations event listeners
    document.getElementById('smtp-config-form')?.addEventListener('submit', handleSaveSmtp);
    document.getElementById('int-smtp-test')?.addEventListener('click', handleTestSmtp);
    document.getElementById('int-smtp-provider')?.addEventListener('change', _onSmtpProviderChange);

    // Email configuration event listeners
    document.getElementById('email-config-form')?.addEventListener('submit', handleSaveEmailConfig);
    document.querySelectorAll('input[name="email-provider"]').forEach(radio => {
        radio.addEventListener('change', toggleSmtpFields);
    });
    document.getElementById('test-email-button')?.addEventListener('click', handleTestEmail);

    // Navigate to Secrets tab when clicking "Configure Secrets" links
    document.getElementById('goto-secrets-brevo')?.addEventListener('click', (e) => {
        e.preventDefault();
        document.getElementById('main-tab-secrets')?.click();
    });
    document.getElementById('goto-secrets-mailgun')?.addEventListener('click', (e) => {
        e.preventDefault();
        document.getElementById('main-tab-secrets')?.click();
    });

    // Stack TLS event listeners
    document.getElementById('stack-tls-save')?.addEventListener('click', handleSaveStackTls);
    document.querySelectorAll('input[name="stack-tls-mode"]').forEach(radio => {
        radio.addEventListener('change', _toggleStackTlsByoFields);
    });

    // Secrets event listeners
    document.getElementById('add-secret-button')?.addEventListener('click', openAddSecretModal);

    // Permission settings form — was unwired, so Save did a native form submit
    // (navigating away to the main page) and never persisted anything.
    document.getElementById('permission-settings-form')?.addEventListener('submit', handleSavePermissionSettings);

    // API Keys event listeners
    document.getElementById('create-api-key-button')?.addEventListener('click', openCreateApiKeyModal);
    document.getElementById('create-api-key-form')?.addEventListener('submit', handleCreateApiKey);
    document.getElementById('cancel-create-api-key')?.addEventListener('click', closeCreateApiKeyModal);
    document.getElementById('copy-api-key-button')?.addEventListener('click', copyNewApiKey);
    document.getElementById('dismiss-new-key-button')?.addEventListener('click', dismissNewKeyDisplay);
    // api-key-token-helper (#5)
    document.getElementById('get-api-token-button')?.addEventListener('click', handleGetApiToken);
    document.getElementById('copy-api-token-button')?.addEventListener('click', copyApiToken);

    // Load email config initially (default tab) - will be called after access verification
}

async function verifyAccess() {
    try {
        currentUser = await me.get();
        isSuperAdmin = currentUser.parent_user_id === null;

        if (!isSuperAdmin) {
            // Non-super-admin trying to access settings
            const pageContent = document.getElementById('page-content');
            if (pageContent) {
                pageContent.innerHTML = `
                    <div class="flex items-center justify-center min-h-screen">
                        <div class="bg-red-50 border border-red-200 rounded-lg p-8 max-w-md">
                            <div class="flex items-center mb-4">
                                <svg class="w-8 h-8 text-red-600 mr-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
                                </svg>
                                <h2 class="text-xl font-bold text-red-800">Access Denied</h2>
                            </div>
                            <p class="text-red-700">
                                You do not have permission to access the Settings page.
                                This page is restricted to super administrators only.
                            </p>
                            <button onclick="window.location.hash = '#home'" class="mt-6 bg-red-600 hover:bg-red-700 text-white px-6 py-2 rounded-lg transition-colors">
                                Return to Home
                            </button>
                        </div>
                    </div>
                `;
            }
            return; // Stop initialization
        }

        // Platform-admin-only tabs in Settings:
        //   - main-tab-ai           (AI Configuration, LDF-4 #1) — now also
        //                            hosts GPU Compatibility as an advanced,
        //                            platform-admin-only sub-tab.
        //   - main-tab-storage      (Storage)
        // (Knowledge Base is its OWN tab, revealed to TENANT admins below.)
        // Integrations stays visible to EVERYONE so any user can reach the
        // Mobile app sub-tab (install + enroll their own per-user login). The
        // SMTP sub-tab inside it is gated to admins in loadIntegrations().
        // Backing /platform endpoints 403 for non-platform-admin either way
        // (defence in depth — UI hide is usability, API is security boundary).
        const isAdmin = !!currentUser.is_platform_admin;
        if (!isAdmin) {
            for (const tabId of ['main-tab-ai', 'main-tab-storage']) {
                const tab = document.getElementById(tabId);
                if (tab) tab.classList.add('hidden');
            }
        }
        // Storage is paid-only: on CE the panel is inert (loadStorage() returns
        // on the same isPaidEdition() guard), so hide the tab like Topology.
        if (!await isPaidEdition()) {
            document.getElementById('main-tab-storage')?.classList.add('hidden');
        }

        // tenant-assistant-knowledge-base: the Knowledge Base is its OWN
        // top-level tab, for TENANT admins (tenant-root OR platform admin) —
        // NOT under the platform-admin-only AI Configuration tab. Reveal it for
        // them; ordinary members never see it. The real boundary is server-side
        // (_is_tenant_admin on every /assistant/knowledge route).
        const _isTenantAdmin = isAdmin || currentUser.parent_user_id === null;
        if (_isTenantAdmin) {
            // FIX-KNOWLEDGE-BASE-IN-CE: the edition gate is GONE — the backend ships on
            // CE (manifest.yaml api_allow :766 assistant_knowledge + deny_exceptions :2455
            // overrides the :1707 deny), so the tab reveals on EVERY edition. The tenant
            // guard above (_isTenantAdmin) is the client-side half; the real boundary is
            // server-side (_is_tenant_admin on every /assistant/knowledge route).
            document.getElementById('main-tab-knowledge')?.classList.remove('hidden');
            // stack-tls — Stack TLS is a TENANT-admin tab, same idiom as
            // Knowledge Base (NOT the platform-admin AI Configuration area).
            // The real boundary is server-side (GET/PUT tenant-tls 403 for
            // non-tenant-admins); the UI hide is usability.
            document.getElementById('main-tab-tls')?.classList.remove('hidden');
            // FIX-CE-HIDE-BYO (FIND-2, owner verdict A): hide the 'Bring your own
            // certificate' radio on CE ONLY (no certificate is ever loaded in
            // CE; the loader is implemented later). The tab itself stays (letsencrypt
            // / vault-acme still apply). Fail-closed isPaidEdition() guard, same
            // idiom as the byok tab just below: unknown = CE = hidden. Paid keeps
            // its original wording and behaviour; the real boundary is server-side
            // (set_tenant_tls 409 + stacks.tls_issuer validator, both inside
            // `if is_ce_edition()`).
            if (!await isPaidEdition()) {
                document.getElementById('stack-tls-mode-byo')?.classList.add('hidden');
                document.querySelector('label[for="stack-tls-mode-byo"]')?.classList.add('hidden');
            }
            // GSEC FX.163 p3b3b — Tenant BYOK is a tenant-admin tab, same idiom
            // as Knowledge Base / Stack TLS. The real boundary is server-side
            // (TENANT:view_settings / TENANT:manage_settings + the BYOK entitlement on
            // /tenant/byok); the UI hide is usability.
            // 2400OPS (PRODRED-B567A): tenant/byok.py is api_deny on CE (carved), so
            // keep the tab hidden there — same fail-closed isPaidEdition() guard as the
            // Knowledge Base tab just above (:233).
            if (await isPaidEdition()) document.getElementById('main-tab-byok')?.classList.remove('hidden');
        }

        // Default tab: platform admin lands on Integrations (where
        // they configure SMTP). Tenant admins land on Secrets since
        // Integrations is hidden for them. The HTML default-active
        // class is on Integrations, so we also need to flip that
        // visually for tenant admins so they don't land on a hidden
        // tab pane.
        if (isAdmin) {
            loadIntegrations();
        } else {
            // Activate Secrets tab visually
            document.querySelectorAll('.main-tab').forEach(t => {
                t.classList.remove('border-primary', 'text-primary', 'active');
                t.classList.add('border-transparent', 'text-gray-500');
                t.setAttribute('aria-selected', 'false');
            });
            const secretsTab = document.getElementById('main-tab-secrets');
            if (secretsTab) {
                secretsTab.classList.add('border-primary', 'text-primary');
                secretsTab.classList.remove('border-transparent', 'text-gray-500');
                secretsTab.setAttribute('aria-selected', 'true');
            }
            document.querySelectorAll('.main-panel').forEach(p => p.classList.add('hidden'));
            const secretsPanel = document.getElementById('main-panel-secrets');
            if (secretsPanel) secretsPanel.classList.remove('hidden');
            loadSecrets();
        }

        // #20 — if the URL carries a sub-tab (a refresh preserved it),
        // restore it over the default computed above. Only honour a tab
        // that actually exists and is visible to this user, so a stale or
        // permission-gated value falls back to the default.
        const wantedTab = getActiveTab();
        if (wantedTab) {
            const wantedEl = document.getElementById(`main-tab-${wantedTab}`);
            if (wantedEl && !wantedEl.classList.contains('hidden')) {
                document.querySelectorAll('.main-tab').forEach(t => t.setAttribute('aria-selected', 'false'));
                wantedEl.setAttribute('aria-selected', 'true');
                wantedEl.click();
            }
        }

    } catch (error) {
        console.error('Error verifying access:', error);
        // On error, show generic error message
        const pageContent = document.getElementById('page-content');
        if (pageContent) {
            pageContent.innerHTML = `
                <div class="flex items-center justify-center min-h-screen">
                    <div class="bg-gray-50 border border-gray-200 rounded-lg p-8 max-w-md">
                        <h2 class="text-xl font-bold text-gray-800 mb-4">Error Loading Settings</h2>
                        <p class="text-gray-700">Unable to verify your permissions. Please try refreshing the page.</p>
                    </div>
                </div>
            `;
        }
    }
}

// ========================================
// Secrets Functions
// ========================================

// Registry secrets for platform admin users only
const HARBOR_SECRETS = [
    {
        name: 'harbor_registry',
        description: '🔧 CONFIGURE ME: Container registry URL (e.g., registry.example.com)',
        placeholder: 'PLEASE_CONFIGURE'
    },
    {
        name: 'harbor_username',
        description: '🔧 CONFIGURE ME: Your registry username for container registry access',
        placeholder: 'PLEASE_CONFIGURE'
    },
    {
        name: 'harbor_password',
        description: '🔧 CONFIGURE ME: Your registry password for container registry access',
        placeholder: 'PLEASE_CONFIGURE'
    }
];

// Platform-tenant-only secrets the wizard / scheduler depend on.
// Auto-seeded as PLEASE_CONFIGURE for platform admins so they
// know to fill them in.
const PLATFORM_ADMIN_SECRETS = [
    {
        name: 'github_token',
        description: '🔧 CONFIGURE ME: GitHub personal access token (read-only, public repos only). Required for the Workflow Builder\'s describe-and-build wizard tab.',
        placeholder: 'PLEASE_CONFIGURE'
    }
];

async function ensureRequiredSecretsExist(existingSecrets, userProfile) {
    const existingNames = new Set(existingSecrets.map(s => s.name));

    // Only create Harbor secrets for platform admins or enterprise users
    if (userProfile && userProfile.has_custom_registry_access) {
        for (const harborSecret of HARBOR_SECRETS) {
            if (!existingNames.has(harborSecret.name)) {
                try {
                    console.log(`Creating placeholder secret: ${harborSecret.name}`);
                    await secrets.create({
                        name: harborSecret.name,
                        value: harborSecret.placeholder,
                        description: harborSecret.description
                    });
                } catch (error) {
                    console.error(`Failed to create placeholder secret ${harborSecret.name}:`, error);
                    // Continue with other secrets even if one fails
                }
            }
        }
    }

    // Platform-admin-only seeds (github_token, future scheduler keys).
    // Reuses the same has_custom_registry_access flag because, in
    // practice, platform admins always have it; tightening to a
    // separate is_platform_admin field would need a backend tweak.
    if (userProfile && userProfile.has_custom_registry_access) {
        for (const platformSecret of PLATFORM_ADMIN_SECRETS) {
            if (!existingNames.has(platformSecret.name)) {
                try {
                    console.log(`Creating placeholder secret: ${platformSecret.name}`);
                    await secrets.create({
                        name: platformSecret.name,
                        value: platformSecret.placeholder,
                        description: platformSecret.description
                    });
                } catch (error) {
                    console.error(`Failed to create placeholder secret ${platformSecret.name}:`, error);
                    // Continue with other secrets even if one fails
                }
            }
        }
    }
}

async function loadSecrets() {
    try {
        // Fetch user profile to check if they have access to custom registry
        const userProfile = await me.get();
        const secretsList = await secrets.get();

        // Auto-create placeholder secrets if they don't exist
        await ensureRequiredSecretsExist(secretsList, userProfile);

        // Reload after creating placeholders
        const updatedSecretsList = await secrets.get();
        renderSecrets(updatedSecretsList);
        attachSecretEventListeners();
    } catch (error) {
        console.error("Error loading secrets:", error);
        const secretsTableBody = document.querySelector("#secrets-table tbody");
        if (secretsTableBody) {
            secretsTableBody.innerHTML = `<tr><td colspan="4" class="text-center text-red-500 py-4">Could not load secrets.</td></tr>`;
        }
    }
}

function renderSecrets(secretsList) {
    const secretsTableBody = document.querySelector("#secrets-table tbody");
    if (!secretsTableBody) return;

    secretsTableBody.innerHTML = '';
    if (secretsList.length === 0) {
        secretsTableBody.innerHTML = `<tr><td colspan="4" class="text-center text-gray-500 py-4">No secrets have been created yet.</td></tr>`;
        return;
    }

    // Sort: unconfigured first, then alphabetically
    const sortedSecrets = [...secretsList].sort((a, b) => {
        const aConfigured = !a.description?.includes('CONFIGURE ME');
        const bConfigured = !b.description?.includes('CONFIGURE ME');
        if (aConfigured !== bConfigured) {
            return aConfigured ? 1 : -1; // Unconfigured first
        }
        return a.name.localeCompare(b.name);
    });

    // Check if any secrets need configuration
    const hasUnconfigured = sortedSecrets.some(secret => secret.description?.includes('CONFIGURE ME'));
    const banner = document.getElementById('required-secrets-banner');
    if (banner) {
        if (hasUnconfigured) {
            banner.classList.remove('hidden');
        } else {
            banner.classList.add('hidden');
        }
    }

    sortedSecrets.forEach(secret => {
        const needsConfig = secret.description?.includes('CONFIGURE ME');
        const rowClass = 'hover:bg-gray-50';
        const statusBadge = needsConfig
            ? '<span class="inline-flex items-center px-2 py-1 rounded text-xs font-medium bg-yellow-200 text-yellow-800">⚠ Not Configured</span>'
            : '<span class="inline-flex items-center px-2 py-1 rounded text-xs font-medium bg-green-200 text-green-800">✓ Configured</span>';

        // Strip the `🔧 CONFIGURE ME: ` sentinel prefix from the
        // description so the cell shows only the substantive copy.
        // The status badge already conveys the "needs configuring"
        // state — the prefix added visual noise that drowned out
        // the actual hint about what the secret is for.
        const cleanDescription = (secret.description || 'No description')
            .replace(/^\s*🔧?\s*CONFIGURE ME:\s*/i, '');

        const row = document.createElement('tr');
        row.className = rowClass;
        row.innerHTML = `
            <td class="px-6 py-4 whitespace-nowrap text-sm font-mono text-gray-900">${secret.name}</td>
            <td class="px-6 py-4 text-sm text-gray-600">${cleanDescription}</td>
            <td class="px-6 py-4 text-center">${statusBadge}</td>
            <td class="px-6 py-4 whitespace-nowrap text-right text-sm space-x-2">
                ${needsConfig ? `<button class="edit-secret-button text-primary hover:text-primary/80 font-medium" data-id="${secret.id}" data-name="${secret.name}">Configure</button>` : ''}
                <button class="delete-secret-button text-red-600 hover:text-red-800" data-id="${secret.id}">Delete</button>
            </td>
        `;
        secretsTableBody.appendChild(row);
    });
}

function attachSecretEventListeners() {
    document.querySelectorAll('.delete-secret-button').forEach(button => {
        button.addEventListener('click', handleDeleteSecret);
    });
    document.querySelectorAll('.edit-secret-button').forEach(button => {
        button.addEventListener('click', handleEditSecret);
    });
}

async function openAddSecretModal() {
    const modal = await loadModal('add-secret');
    if (modal) {
        const form = modal.querySelector('#add-secret-form');
        const cancelButton = modal.querySelector('#cancel-secret-button');

        // Password visibility toggle (parity with secrets.js — this modal is
        // injected via innerHTML, so the eye button is inert unless a handler
        // is attached here).
        const toggleButton = modal.querySelector('#toggle-secret-visibility');
        const secretInput = modal.querySelector('#secret-value');
        const eyeIcon = modal.querySelector('#eye-icon');
        const eyeOffIcon = modal.querySelector('#eye-off-icon');
        if (toggleButton && secretInput && eyeIcon && eyeOffIcon) {
            toggleButton.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                if (secretInput.type === 'password') {
                    secretInput.type = 'text';
                    eyeIcon.classList.add('hidden');
                    eyeOffIcon.classList.remove('hidden');
                } else {
                    secretInput.type = 'password';
                    eyeIcon.classList.remove('hidden');
                    eyeOffIcon.classList.add('hidden');
                }
            };
        }

        form.onsubmit = async (event) => {
            event.preventDefault();
            const name = document.getElementById('secret-name').value;
            const value = document.getElementById('secret-value').value;
            const description = document.getElementById('secret-description').value;
            try {
                await secrets.create({ name, value, description });
                closeModal('add-secret-modal');
                loadSecrets(); // Refresh the secrets list
            } catch (error) {
                console.error("Error creating secret:", error);
                showError(`Failed to create secret: ${error.message}`);
            }
        };

        cancelButton.onclick = () => closeModal('add-secret-modal');
    }
}

async function handleEditSecret(event) {
    const secretId = event.target.getAttribute('data-id');
    const secretName = event.target.getAttribute('data-name');

    // Prompt user for new value
    const newValue = prompt(`Enter the value for ${secretName}:`, '');

    if (newValue && newValue.trim() !== '' && newValue !== 'PLEASE_CONFIGURE') {
        try {
            // Delete old secret and create new one with same name
            await secrets.delete(secretId);
            await secrets.create({
                name: secretName,
                value: newValue.trim(),
                description: getSecretDescription(secretName)
            });
            loadSecrets(); // Refresh the list
        } catch (error) {
            console.error("Error updating secret:", error);
            showError(`Failed to update secret: ${error.message}`);
        }
    } else if (newValue !== null) {
        showInfo('Please enter a valid value for this secret.');
    }
}

function getSecretDescription(secretName) {
    const descriptions = {
        'harbor_registry': 'Harbor container registry URL',
        'harbor_username': 'Harbor username for container registry access',
        'harbor_password': 'Harbor password for container registry access',
        'github_token': 'GitHub PAT (read-only public_repo) — gates the wizard\'s describe-and-build tab'
    };
    return descriptions[secretName] || 'Custom secret';
}

async function handleDeleteSecret(event) {
    const secretId = event.target.getAttribute('data-id');
    if (await showConfirm('Are you sure you want to delete this secret? This action cannot be undone.')) {
        try {
            await secrets.delete(secretId);
            loadSecrets(); // Refresh the list
        } catch (error) {
            console.error("Error deleting secret:", error);
            showError("Failed to delete the secret.");
        }
    }
}

// ========================================
// Shared Settings Helpers
// ========================================

const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';

// Helper functions
function formatDate(dateString) {
    if (!dateString) return 'N/A';
    const date = new Date(dateString);
    return date.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}

// ========================================
// API Keys Functions
// ========================================

let newlyCreatedApiKey = null;

async function loadApiKeys() {
    const tbody = document.getElementById('api-keys-tbody');
    if (!tbody) return;

    try {
        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="text-center py-8 text-gray-500">
                    <svg class="animate-spin h-6 w-6 mx-auto mb-2 text-primary" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    Loading API keys...
                </td>
            </tr>
        `;

        const keysList = await apiKeys.get();
        renderApiKeys(keysList);
    } catch (error) {
        console.error('Error loading API keys:', error);
        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="text-center py-8 text-red-500">
                    Failed to load API keys: ${error.message}
                </td>
            </tr>
        `;
    }
}

function renderApiKeys(keysList) {
    const tbody = document.getElementById('api-keys-tbody');
    if (!tbody) return;

    if (!keysList || keysList.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="text-center py-8 text-gray-500">
                    <svg class="w-12 h-12 mx-auto mb-3 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"/>
                    </svg>
                    <p class="font-medium">No API keys yet</p>
                    <p class="text-sm mt-1">Create your first API key to get started with programmatic access.</p>
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = keysList.map(key => `
        <tr class="hover:bg-gray-50 cursor-pointer apikey-row" data-key-id="${key.id}" data-key-name="${_skEsc(key.key_prefix || '')}...">
            <td class="px-6 py-4 whitespace-nowrap">
                <code class="bg-gray-100 px-2 py-1 rounded text-sm font-mono">${_skEsc(key.key_prefix)}...</code>
            </td>
            <td class="px-6 py-4 text-sm text-gray-900">
                ${key.description ? _skEsc(key.description) : '<span class="text-gray-400">No description</span>'}
                ${key.stack_id != null ? `<span class="inline-flex items-center rounded bg-amber-100 px-1 py-0.5 text-xs font-semibold text-amber-800">Stack deploy: ${_skEsc(key.stack_name || ('#' + key.stack_id))}</span>` : ''}
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                ${formatDate(key.created_at)}
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                ${key.last_used_at ? formatDate(key.last_used_at) : '<span class="text-gray-400">Never</span>'}
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                ${key.expires_at ? formatDate(key.expires_at) : '<span class="text-gray-400">Never</span>'}
                ${key.stale ? '<span class="inline-flex items-center rounded bg-amber-100 px-1 py-0.5 text-xs font-semibold text-amber-800">Stale</span>' : ''}
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-right">
                <button class="rotate-api-key-btn text-amber-600 hover:text-amber-800 text-sm font-medium" data-key-id="${key.id}">
                    Rotate
                </button>
                <button class="revoke-api-key-btn text-red-600 hover:text-red-800 text-sm font-medium" data-key-id="${key.id}">
                    Revoke
                </button>
            </td>
        </tr>
    `).join('');

    // Attach event listeners for revoke buttons
    document.querySelectorAll('.revoke-api-key-btn').forEach(btn => {
        btn.addEventListener('click', handleRevokeApiKey);
    });

    // FX.152 p4 — rotate buttons (new key shown once, old key keeps a grace window)
    document.querySelectorAll('.rotate-api-key-btn').forEach(btn => {
        btn.addEventListener('click', handleRotateApiKey);
    });

    document.querySelectorAll('.apikey-row').forEach(row => {
        row.addEventListener('click', (e) => {
            if (e.target.tagName === 'BUTTON') return;
            document.querySelectorAll('.apikey-row').forEach(r => r.classList.remove('bg-primary/20'));
            row.classList.add('bg-primary/20');
            loadResourcePermissions('apikey-permissions-panel', 'api_key', parseInt(row.dataset.keyId), row.dataset.keyName);
        });
    });
}

async function openCreateApiKeyModal() {
    const modal = document.getElementById('create-api-key-modal');
    if (modal) {
        modal.classList.remove('hidden');
        modal.classList.add('flex');
        // Reset form
        document.getElementById('api-key-description').value = '';
        document.getElementById('api-key-expires').value = '';
        // Reset scope / read-only controls so a prior key's choices never leak
        // into the next create.
        const readOnlyEl = document.getElementById('api-key-read-only');
        if (readOnlyEl) readOnlyEl.checked = false;
        const customScopeEl = document.getElementById('api-key-scope-custom');
        if (customScopeEl) customScopeEl.value = '';
        document.querySelectorAll('.api-key-scope-check').forEach((cb) => { cb.checked = false; });
        document.querySelectorAll('.api-key-stack-scope-check').forEach((cb) => { cb.checked = false; });
        const stackSelect = document.getElementById('api-key-stack-id');
        if (stackSelect) stackSelect.value = '';
        setApiKeyStackHint('');
        await populateApiKeyStackOptions();
        // Re-arm the submit button in case a previous attempt left it disabled.
        const submitBtn = document.getElementById('submit-create-api-key');
        if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Create Key'; }
        // Set minimum date to tomorrow
        const tomorrow = new Date();
        tomorrow.setDate(tomorrow.getDate() + 1);
        document.getElementById('api-key-expires').min = tomorrow.toISOString().split('T')[0];
    }
}

function closeCreateApiKeyModal() {
    const modal = document.getElementById('create-api-key-modal');
    if (modal) {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
    }
}

// TOKMERGE J5 — stack deploy keys: the picker options are built with
// createElement + textContent/value (never innerHTML), so a stack named with
// markup renders as literal text. On a failed /stacks fetch the select keeps
// only the placeholder option and the inline hint explains why; the create
// handler then refuses any stack scope (nothing is sent).
async function populateApiKeyStackOptions() {
    const select = document.getElementById('api-key-stack-id');
    if (!select) return;
    setApiKeyStackHint('');
    while (select.firstChild) select.removeChild(select.firstChild);
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Select a stack';
    select.appendChild(placeholder);
    // D75 (): the /stacks read used to run ONCE per modal open, so
    // a failed or early fetch left the select placeholder-only for the whole
    // create flow. Now: 3 attempts, 1.5 s apart. A failed read is never
    // treated as an empty list, and the hint says "could not load", never
    // "no stacks".
    let rows;
    let fetched = false;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
        try {
            rows = await apiCall('/stacks');
            fetched = true;
            break;
        } catch (e) {
            if (attempt === 3) break;
            setApiKeyStackHint('Could not load your stacks: retrying');
            await new Promise(r => setTimeout(r, 1500));
        }
    }
    if (!fetched) {
        setApiKeyStackHint('Could not load your stacks. Reload to try again.');
        return;
    }
    setApiKeyStackHint('');
    const list = Array.isArray(rows) ? rows : (rows && Array.isArray(rows.stacks) ? rows.stacks : []);
    list.forEach((s) => {
        if (!s || s.status === 'torn_down') return;
        const sid = s.id == null ? null : Number(s.id);
        if (!Number.isFinite(sid)) return;
        const opt = document.createElement('option');
        opt.value = String(sid);
        opt.textContent = (s.name ? String(s.name) : '#' + sid);
        select.appendChild(opt);
    });
}

function setApiKeyStackHint(text) {
    const hint = document.getElementById('api-key-stack-hint');
    if (hint) hint.textContent = text || '';
}

async function handleCreateApiKey(event) {
    event.preventDefault();

    // Double-submit lockout: disable the button on first click and keep it
    // disabled until the request resolves, so a rapid double-click mints only
    // ONE key. Guard re-entrancy too in case the event fires twice.
    const submitBtn = document.getElementById('submit-create-api-key');
    if (submitBtn && submitBtn.disabled) return;
    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Creating…'; }

    const description = document.getElementById('api-key-description').value.trim();
    const expiresInput = document.getElementById('api-key-expires').value;

    const data = { description };
    if (expiresInput) {
        // Convert to ISO string with time
        data.expires_at = new Date(expiresInput).toISOString();
    }

    // Read-only flag (is_read_only) — server-enforced at authorization time.
    const readOnlyEl = document.getElementById('api-key-read-only');
    if (readOnlyEl && readOnlyEl.checked) data.is_read_only = true;

    // Capability scope (capability_scope) — checked known scopes plus any
    // free-form resource:capability pairs. Omitted entirely when empty so the
    // key stays full-access.
    const scopes = [];
    document.querySelectorAll('.api-key-scope-check:checked').forEach((cb) => {
        if (cb.value) scopes.push(cb.value);
    });
    const customEl = document.getElementById('api-key-scope-custom');
    if (customEl && customEl.value.trim()) {
        customEl.value.split(/[\s,]+/).forEach((s) => {
            const v = s.trim();
            if (v && !scopes.includes(v)) scopes.push(v);
        });
    }
    // Stack deploy scopes are a separate group: they pin the key to ONE stack
    // (stack_id) and must not mix with ordinary/custom scopes or read-only.
    // Fail closed: on any mismatch show the inline hint and send NOTHING.
    const stackScopes = [];
    document.querySelectorAll('.api-key-stack-scope-check:checked').forEach((cb) => {
        if (cb.value) stackScopes.push(cb.value);
    });
    const stackSelectEl = document.getElementById('api-key-stack-id');
    const stackPicked = stackSelectEl && String(stackSelectEl.value || '').trim() !== '';
    if (stackScopes.length) {
        if (!stackPicked) {
            setApiKeyStackHint('Pick the stack this deploy key is pinned to, or uncheck the stack scopes.');
            if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Create Key'; }
            return;
        }
        if (scopes.length || (customEl && customEl.value.trim()) || (readOnlyEl && readOnlyEl.checked)) {
            setApiKeyStackHint('Stack deploy keys carry only stack scopes');
            if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Create Key'; }
            return;
        }
        data.capability_scope = stackScopes;
        data.stack_id = Number(stackSelectEl.value);
    } else if (stackPicked) {
        setApiKeyStackHint('Check at least one stack scope to pin this key, or clear the stack picker.');
        if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Create Key'; }
        return;
    }
    if (scopes.length) data.capability_scope = scopes;

    try {
        const result = await apiKeys.create(data);
        closeCreateApiKeyModal();

        // Store the new key and show it
        newlyCreatedApiKey = result.access_key;
        showNewKeyDisplay(result.access_key);

        // Refresh the list
        await loadApiKeys();
    } catch (error) {
        console.error('Error creating API key:', error);
        showError(`Failed to create API key: ${error.message}`);
    } finally {
        // Re-arm the button whether we succeeded (modal closed) or failed (user
        // can retry). One intent = one request.
        if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Create Key'; }
    }
}

function showNewKeyDisplay(keyValue) {
    const display = document.getElementById('new-api-key-display');
    const valueEl = document.getElementById('new-api-key-value');
    if (display && valueEl) {
        valueEl.textContent = keyValue;
        display.classList.remove('hidden');
    }
    // api-key-token-helper (#5): reset any token from a previous key.
    resetApiTokenResult();
}

function dismissNewKeyDisplay() {
    const display = document.getElementById('new-api-key-display');
    if (display) {
        display.classList.add('hidden');
    }
    newlyCreatedApiKey = null;
    // api-key-token-helper (#5): clear the exchanged token from the DOM so the
    // short-lived secret doesn't linger after the panel is dismissed.
    resetApiTokenResult();
}

// api-key-token-helper (#5): clear the on-screen bearer token.
function resetApiTokenResult() {
    const result = document.getElementById('api-token-result');
    const valueEl = document.getElementById('api-token-value');
    if (valueEl) valueEl.textContent = '';
    if (result) result.classList.add('hidden');
}

// api-key-token-helper (#5): exchange the just-created raw key for a
// short-lived bearer token via POST /api-keys/token and show it. The raw key
// is only available client-side at creation time (never stored), so this is
// the one moment a working "Get token" button can run.
async function handleGetApiToken() {
    if (!newlyCreatedApiKey) {
        showError('The API key is no longer available. Create a new key to get a token.');
        return;
    }
    const btn = document.getElementById('get-api-token-button');
    const original = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Getting token...'; }
    try {
        const result = await apiKeys.exchangeForToken(newlyCreatedApiKey);
        const valueEl = document.getElementById('api-token-value');
        const expiryEl = document.getElementById('api-token-expiry');
        const resultEl = document.getElementById('api-token-result');
        if (valueEl) valueEl.textContent = result.access_token || '';
        if (expiryEl && result.expires_in) {
            const mins = Math.round(result.expires_in / 60);
            expiryEl.textContent = `expires in ${mins} minute${mins !== 1 ? 's' : ''}`;
        }
        if (resultEl) resultEl.classList.remove('hidden');
    } catch (error) {
        console.error('Error exchanging API key for token:', error);
        showError(`Failed to get bearer token: ${error.message}`);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = original; }
    }
}

// api-key-token-helper (#5): copy the exchanged bearer token to the clipboard.
async function copyApiToken() {
    const tokenValue = document.getElementById('api-token-value')?.textContent;
    if (!tokenValue) return;
    try {
        // Shared copy helper (js/utils.js): navigator.clipboard is undefined
        // over plain http on the LAN, so the old call threw into the catch and
        // the button never copied. Same text; the helper's result drives the
        // existing success/failure feedback below.
        const ok = await window.modCopyText(tokenValue);
        if (!ok) throw new Error('clipboard copy failed');
        const btn = document.getElementById('copy-api-token-button');
        if (btn) {
            const originalText = btn.textContent;
            btn.textContent = 'Copied!';
            btn.classList.add('bg-gray-600');
            setTimeout(() => {
                btn.textContent = originalText;
                btn.classList.remove('bg-gray-600');
            }, 2000);
        }
    } catch (error) {
        console.error('Failed to copy token:', error);
        showInfo('Press Ctrl+C to copy the token');
    }
}

async function copyNewApiKey() {
    const keyValue = document.getElementById('new-api-key-value')?.textContent;
    if (!keyValue) return;

    try {
        // Shared copy helper (js/utils.js): same reason as copyApiToken - the
        // old call threw on plain http (LAN installs) and only the select-the-
        // text fallback ran. Same text; the helper's result drives the
        // existing success/failure feedback below.
        const ok = await window.modCopyText(keyValue);
        if (!ok) throw new Error('clipboard copy failed');
        const btn = document.getElementById('copy-api-key-button');
        if (btn) {
            const originalText = btn.textContent;
            btn.textContent = 'Copied!';
            btn.classList.remove('bg-primary/60', 'hover:bg-primary/90');
            btn.classList.add('bg-gray-500');
            setTimeout(() => {
                btn.textContent = originalText;
                btn.classList.add('bg-primary/60', 'hover:bg-primary/90');
                btn.classList.remove('bg-gray-500');
            }, 2000);
        }
    } catch (error) {
        console.error('Failed to copy API key:', error);
        // Fallback: select the text
        const valueEl = document.getElementById('new-api-key-value');
        if (valueEl) {
            const range = document.createRange();
            range.selectNodeContents(valueEl);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
        }
        showInfo('Press Ctrl+C to copy the API key');
    }
}

async function handleRevokeApiKey(event) {
    const keyId = event.target.getAttribute('data-key-id');
    if (!keyId) return;

    if (!await showConfirm('Are you sure you want to revoke this API key? This action cannot be undone. Any applications using this key will lose access immediately.')) {
        return;
    }

    try {
        await apiKeys.revoke(keyId);
        await loadApiKeys();
    } catch (error) {
        console.error('Error revoking API key:', error);
        showError(`Failed to revoke API key: ${error.message}`);
    }
}

// FX.152 p4 — rotate an API key: POST /api-keys/{id}/rotate issues a new key
// (shown exactly once through the same reveal/copy display the create flow
// uses) and keeps the old key alive for the API's grace window.
async function handleRotateApiKey(event) {
    const keyId = event.target.getAttribute('data-key-id');
    if (!keyId) return;

    if (!await showConfirm('Rotate this API key? A new key will be issued and the old key will stop working after its grace window. Update any applications using the old key before then.')) {
        return;
    }

    const btn = event.target;
    if (btn) { btn.disabled = true; btn.textContent = 'Rotating…'; }
    try {
        const result = await apiKeys.rotate(keyId);
        // BUG.B43 — the "Get bearer token" button exchanges whatever raw key the
        // client is still holding. Rotate mints a new key and soft-revokes the
        // old one, so the pre-rotate plaintext (or a stale null) is now dead:
        // hand the new key to the same one-time holder the create flow uses.
        newlyCreatedApiKey = result.access_key;
        showNewKeyDisplay(result.access_key);
        showInfo(result.grace_until
            ? `Rotation complete. The old key stays valid until ${formatDate(result.grace_until)}, then it stops working.`
            : 'Rotation complete. The old key stops working after its grace window.');
        await loadApiKeys();
    } catch (error) {
        console.error('Error rotating API key:', error);
        showError(`Failed to rotate API key: ${error.message}`);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Rotate'; }
    }
}


// GE.AIC.5 (2026-09-29): the "GPU Compatibility" tab/panel and its
// hardware-tag editor (HW_TAG_LABELS, loadGPUSettings, renderHardwareTags,
// handleAddHardwareTag, handleSaveGPUBackends) were removed from AI
// Configuration — "not really needed as we do the builds based on the
// accelerators available" (owner). Every backend reader now derives
// accelerator classes from the live worker fleet instead of the
// PlatformGPUSettings row this UI used to edit (see
// node_builder/build_dag/generate_env.py:_fleet_hw_tags). The
// GET/PATCH /accelerators/backends API is kept working (deprecated) for any
// external caller, but nothing in the Dashboard calls it anymore.

// --- Permission Settings (tenant) — GET/PUT /tenant/permission-settings ---
async function loadPermissionSettings() {
    try {
        const s = await apiCall('/tenant/permission-settings');
        if (!s) return;
        const ws = document.getElementById('warning-schedule');
        if (ws) ws.value = (s.warning_schedule || []).join(', ');
        const emailChk = document.getElementById('email-notifications-enabled');
        if (emailChk) emailChk.checked = !!s.email_notifications_enabled;
        const internal = document.getElementById('default-internal-duration');
        if (internal) internal.value = s.default_editor_duration ?? s.default_viewer_duration ?? '';
        const external = document.getElementById('default-external-duration');
        if (external) external.value = s.default_external_user_duration ?? '';
        const autoRevoke = document.getElementById('auto-revoke-enabled');
        if (autoRevoke) autoRevoke.checked = !!s.auto_revoke_expired;
        const grace = document.getElementById('auto-revoke-grace-period');
        if (grace) grace.value = s.grace_period_hours ?? '';
    } catch (e) {
        console.error('Failed to load permission settings:', e);
    }
}

async function handleSavePermissionSettings(event) {
    event.preventDefault();
    const ws = (document.getElementById('warning-schedule')?.value || '')
        .split(',').map(x => parseInt(x.trim(), 10)).filter(n => !isNaN(n));
    const internal = parseInt(document.getElementById('default-internal-duration')?.value, 10);
    const external = parseInt(document.getElementById('default-external-duration')?.value, 10);
    const grace = parseInt(document.getElementById('auto-revoke-grace-period')?.value, 10);

    const body = {
        warning_schedule: ws,
        email_notifications_enabled: !!document.getElementById('email-notifications-enabled')?.checked,
        auto_revoke_expired: !!document.getElementById('auto-revoke-enabled')?.checked,
    };
    // The form has one "internal" duration; the backend splits editor/viewer —
    // apply the same value to both.
    if (!isNaN(internal)) { body.default_editor_duration = internal; body.default_viewer_duration = internal; }
    if (!isNaN(external)) body.default_external_user_duration = external;
    if (!isNaN(grace)) body.grace_period_hours = grace;

    const btn = event.submitter || document.querySelector('#permission-settings-form button[type="submit"]');
    const prev = btn ? btn.textContent : null;
    if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
    try {
        await apiCall('/tenant/permission-settings', { method: 'PUT', body: JSON.stringify(body) });
        showInfo('Permission settings saved.');
        await loadPermissionSettings();
    } catch (e) {
        showError('Failed to save permission settings: ' + (e?.message || e));
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = prev; }
    }
}

// =========================================================================
// AI Configuration — per-tool, four execution modes
// =========================================================================

const AI_TOOLS = ['wizard', 'adapter', 'assistant', 'repair'];

const PROVIDER_HINTS = {
    openai: '<strong>OpenAI:</strong> Get your API key from <a href="https://platform.openai.com/api-keys" target="_blank" class="underline">platform.openai.com</a>. Recommended model: <code>gpt-4o-mini</code> or <code>gpt-4o</code>',
    anthropic: '<strong>Anthropic:</strong> Get your API key from <a href="https://console.anthropic.com/" target="_blank" class="underline">console.anthropic.com</a>. Recommended model: <code>claude-sonnet-4-20250514</code>',
    google: '<strong>Google:</strong> Get your API key from <a href="https://aistudio.google.com/apikey" target="_blank" class="underline">aistudio.google.com</a>. Recommended model: <code>gemini-2.0-flash</code>',
    groq: '<strong>Groq:</strong> Get your API key from <a href="https://console.groq.com/" target="_blank" class="underline">console.groq.com</a>. Recommended model: <code>llama-3.1-70b-versatile</code>',
    mistral: '<strong>Mistral:</strong> Get your API key from <a href="https://console.mistral.ai/" target="_blank" class="underline">console.mistral.ai</a>. Recommended model: <code>mistral-large-latest</code>',
};

const MODE_HELP = {
    // §3.4 — say what each mode makes YOU responsible for, not just what it is.
    //
    // "Local" no longer means picking a model: the hardware tier decides what
    // runs, and this tool takes the role it needs from whatever that tier
    // serves. That is why there is no model dropdown here.
    local: 'MOD runs the models for you. The hardware tier decides which model '
         + 'serves this tool — change tier and this follows automatically.',
    self_hosted: 'Point MOD at your own Ollama instance. You run the model; MOD '
         + 'cannot tell whether it is healthy or large enough.',
    cloud: 'Use an external provider like OpenAI, Anthropic, or Google. Calls '
         + 'leave your infrastructure and are billed by that provider.',
    advanced: 'You choose the model, not the tier. MOD will not check that your '
         + 'choice can be served, and will not re-point it when the tier '
         + 'changes — including to a model no worker here can run.',
};

// In-memory stash per tool so typed values survive mode switches within a session.
// `critic` is the Node Wizard's separate reviewer/reasoner pseudo-tool — not a
// top-level tab, but persisted like one when "Separate reasoner model" is chosen.
const _aiFormState = { wizard: {}, adapter: {}, assistant: {}, repair: {}, critic: {} };

// Ollama service health — populated at load. Drives the Local option's
// availability in every tool's dropdown.
let _ollamaHealth = { healthy: false, base_url: '', served_models: [], saved_preset: null, running_matches_saved: true, error: null };

// Model catalog from GET /platform/ai-config/model-catalog.
//
// ai-config-stack-binding §7.3b — this read is now READ-ONLY. The AI page no
// longer offers a "which local model" picker: model choice moved to
// Persistent Stacks → "New AI stack", and the only way to ask for MOD's own
// pick is a slot set to "Let MOD choose" (§7.2 slots table). What this read
// feeds now is prose: the §7.3b tier reading in the master bar names the
// models MOD will pick, and _toolSummaryText / _updateModelSummary label the
// model a tool is already using. `available:false` entries stay catalogued so
// the prose can say when a model has no endpoint stood up yet.
let _modelCatalog = { default: 'devstral-small-2', models: [] };

function _modelCatalogByKey(key) {
    return (_modelCatalog.models || []).find((m) => m.key === key) || null;
}

// The memory a model needs, in GB — the CARD floor (vram_floor_gb) the fleet is
// judged against, falling back to the weight estimate. Returned as a number so
// callers can format it uniformly; null when the catalog carries no figure.
function _modelMemGb(m) {
    if (!m) return null;
    const v = (m.vram_floor_gb != null) ? m.vram_floor_gb
            : (m.vram_est_gb != null) ? m.vram_est_gb : null;
    return (v == null) ? null : Number(v);
}

// A dropdown option label that is ONLY the model and the memory it needs, e.g.
// "gpt-oss-20b — 22.5 GB". The owner's brief: every model dropdown lists models,
// each labelled with its memory requirement, and nothing else.
function _modelMemLabel(m) {
    if (!m) return '';
    const gb = _modelMemGb(m);
    const name = m.key || m.served_name || m.label || 'model';
    return gb == null ? `${name} — memory unknown` : `${name} — ${gb} GB`;
}

// §7.3b — the per-tool model picker is GONE. Nothing on this page chooses a
// local model any more: the model comes from the stack a slot is bound to
// (§7.2 slots table) or from whatever MOD picks for "Let MOD choose". The
// catalog read survives (it feeds the read-only tier sentence and the
// per-tool "model in use" summaries), but the control it used to fill does
// not exist, so this is a documented no-op rather than a dropdown builder.
function _populateModelSelect() { /* §7.3b — no model picker left on this page */ }

function _updateModelSummary(form, modelKey) {
    const help = form.querySelector('[data-field-help="local_model_summary"]');
    if (!help) return;
    const m = _modelCatalogByKey(modelKey);
    if (!m) { help.textContent = ''; return; }
    let s = m.vendor || '';
    if (m.role_hint) s += ` · ${m.role_hint}`;
    if (m.vram_est_gb) s += ` · ~${m.vram_est_gb} GB VRAM`;
    if (m.vram_floor_gb) s += ` · needs a ${m.vram_floor_gb} GB card`;
    help.textContent = s;
    if (m.available === false) {
        help.innerHTML = `${_esc(s)}<div class="text-destructive mt-0.5">` +
            `${_esc(m.unavailable_reason || "No worker in this fleet can host this model.")}` +
            `</div><div class="text-gray-600">Saving stores the choice; calls will ` +
            `fail until the fleet can host it.</div>`;
    } else if (m.available === null) {
        help.innerHTML = `${_esc(s)}<div class="text-warning mt-0.5">` +
            `Fleet capacity could not be determined, so whether this model can ` +
            `run here is unknown.</div>`;
    }
}

// The one-line summary shown on a collapsed Wizard card: the model this tool
// uses and the memory it needs, so the four cards are legible without expanding
// any of them.
function _toolSummaryText(tool) {
    const form = document.querySelector(`#ai-tool-${tool} form`);
    const state = _aiFormState[tool] || {};
    const mode = form
        ? form.querySelector('[data-field="execution_mode"]').value
        : (state.execution_mode || 'local');
    if (mode === 'cloud') {
        return `Cloud · ${state.provider || 'provider'}${state.model ? ' / ' + state.model : ''}`;
    }
    if (mode === 'self_hosted') {
        return `Self-hosted · ${state.model || 'gpt-oss:20b'}`;
    }
    if (mode === 'advanced') {
        const m = _modelCatalogByKey(state.local_model);
        return m ? _modelMemLabel(m) : (state.model || state.local_model || 'custom model');
    }
    // local (managed): the hardware tier serves a model per role. Show the model
    // bound to this tool's role, with the memory that model needs.
    const preset = state.local_preset || (_tierLadder && _tierLadder.default);
    const t = _tier(preset);
    const roles = (t && t.placement && t.placement.roles) || [];
    const role = roles.find((r) => r.role === state.local_role) || roles[0];
    if (role && role.model) {
        const mem = (role.vram_floor_gb != null) ? ` — ${role.vram_floor_gb} GB` : '';
        return `${role.model}${mem}`;
    }
    if (state.resolved_role_model) return state.resolved_role_model;
    const cm = _modelCatalogByKey(state.local_model);
    return cm ? _modelMemLabel(cm) : 'Managed';
}

function _updateToolSummary(tool) {
    const el = document.querySelector(`[data-tool-card="${tool}"] [data-role="summary-model"]`);
    if (el) el.textContent = _toolSummaryText(tool);
}

// ai-configuration-page redesign — per-tool primary-section labelling. Each
// tool's first (full) model block is named for the role it plays.
const _TOOL_PRIMARY_SECTION = {
    wizard:    { heading: 'Builder model',    sub: 'The model that writes new node code.' },
    adapter:   { heading: 'Model',            sub: 'The model that wraps an existing container into a node.' },
    assistant: { heading: 'Chat model',       sub: 'The model that answers in the AI Assistant.' },
    repair:    { heading: 'Repair model',     sub: 'The model that diagnoses and fixes failed builds.' },
};

function _renderToolSubPanel(tool) {
    const container = document.getElementById(`ai-tool-${tool}`);
    const template = document.getElementById('ai-tool-template');
    if (!container || !template) return;
    if (container.querySelector('form')) return; // already rendered

    const clone = template.content.cloneNode(true);
    const form = clone.querySelector('form');
    form.dataset.tool = tool;

    // Per-tool primary-section heading (Generator / Chat / Repair reasoner / …).
    const meta = _TOOL_PRIMARY_SECTION[tool];
    if (meta) {
        const h = clone.querySelector('[data-role="primary-heading"]');
        const s = clone.querySelector('[data-role="primary-sub"]');
        if (h) h.textContent = meta.heading;
        if (s) s.textContent = meta.sub;
    }

    // The "Review reasoner" (critic_mode) section is Node-Wizard-only — remove it
    // from every other tool's panel so it neither shows nor gets saved.
    if (tool !== 'wizard') {
        clone.querySelectorAll('[data-wizard-only]').forEach((el) => el.remove());
    }
    // The "follows the Node Wizard" note is Container-Adapter-only.
    if (tool === 'adapter') {
        clone.querySelectorAll('[data-adapter-only]').forEach((el) => el.classList.remove('hidden'));
    } else {
        clone.querySelectorAll('[data-adapter-only]').forEach((el) => el.remove());
    }
    // The two-model ("proposer + fast fix-reviewer") note is Build-Repair-only.
    if (tool === 'repair') {
        clone.querySelectorAll('[data-repair-only]').forEach((el) => el.classList.remove('hidden'));
    } else {
        clone.querySelectorAll('[data-repair-only]').forEach((el) => el.remove());
    }

    // GE.MNL.4b — the fallback checkbox/select come from the shared template
    // with no id (it's cloned per tool); give each instance its own stable,
    // prefixed id here so labels and E2E selectors can address one tool's
    // control without colliding with its siblings.
    const fbEnabled = clone.querySelector('[data-role="fallback-enabled"]');
    const fbEnabledLabel = clone.querySelector('[data-role="fallback-enabled-label"]');
    const fbTool = clone.querySelector('[data-role="fallback-tool"]');
    const fbToolLabel = clone.querySelector('[data-role="fallback-tool-label"]');
    if (fbEnabled) fbEnabled.id = `ai-fallback-enabled-${tool}`;
    if (fbEnabledLabel && fbEnabled) fbEnabledLabel.setAttribute('for', fbEnabled.id);
    if (fbTool) fbTool.id = `ai-fallback-tool-${tool}`;
    if (fbToolLabel && fbTool) fbToolLabel.setAttribute('for', fbTool.id);

    form.addEventListener('submit', (e) => handleSaveAllAIConfig(e));
    // Keep in-memory state current as any field changes.
    form.addEventListener('change', (e) => {
        _captureFormState(tool);
        // separate-reasoner-model — the Node Wizard also carries the separate
        // reviewer/reasoner panel: capture its fields and toggle its visibility
        // when the code-review mode changes.
        if (tool === 'wizard') {
            _captureCriticState(form);
            _applyCriticMode(form);
        }
        // GE.MNL.4b — the target select is only meaningful while the checkbox
        // is on; keep it disabled/enabled in lockstep and stash whichever
        // changed into the tool's in-memory state.
        if (e.target && (e.target.dataset.role === 'fallback-enabled' || e.target.dataset.role === 'fallback-tool')) {
            _applyFallbackToggle(form);
            _captureFallbackState(tool, form);
        }
        // GE.AIC.1-3 — cheap/idempotent: re-run on every change so switching
        // either Provider select (primary or, for wizard, the critic panel's)
        // shows/hides the Stack picker immediately, and switching the Stack
        // picker itself refreshes the served-model options.
        _applyStackProviderToggle(form);
    });

    container.appendChild(clone);
}

function _applyMode(tool, mode) {
    const form = document.querySelector(`#ai-tool-${tool} form`);
    if (!form) return;
    form.querySelectorAll('[data-mode]').forEach((el) => {
        el.classList.toggle('hidden', el.dataset.mode !== mode);
    });
    const help = form.querySelector('[data-field-help="execution_mode"]');
    if (help) {
        const cfg = _aiFormState[tool] || {};
        // §3.6 — a binding that no longer resolves is stated against the tool.
        // Silence here means the tool answers from a fallback model and nobody
        // knows the configured one stopped applying.
        const bad = cfg.role_unresolved_reason;
        const bound = (mode === 'local' && cfg.local_role)
            ? `<div class="text-gray-600 mt-0.5">This tool takes the ` +
              `<strong>${_esc(cfg.local_role)}</strong> role` +
              (cfg.resolved_role_model ? `, currently ${_esc(cfg.resolved_role_model)}` : '') +
              `.</div>` : '';
        help.innerHTML = _esc(MODE_HELP[mode] || '') + bound +
            (bad ? `<div class="text-destructive mt-0.5">${_esc(bad)}</div>` : '');
    }
    // Re-populate whichever block is now visible from the stashed state
    _hydrateForm(tool, _aiFormState[tool]);
}

function _captureFormState(tool) {
    const form = document.querySelector(`#ai-tool-${tool} form`);
    if (!form) return;
    const state = _aiFormState[tool];
    form.querySelectorAll('[data-field]').forEach((el) => {
        const key = el.dataset.field;
        if (!key || key === 'execution_mode') return;
        if (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') {
            state[key] = el.value;
        }
    });
}

// The preset ladder, served by GET /platform/ai-config/tiers.
//
// This was a hand-maintained mirror of the server's LOCAL_PRESETS, with three
// rungs and their VRAM figures typed in by hand. It went stale the moment the
// `cpu` and `minimal` rungs landed: two tiers that existed and worked could not
// be selected here at all, and the numbers shown for the other three were only
// as current as the last person who remembered to edit both files.
//
// Nothing in this file recomputes any of it. `tier_capabilities()` already does
// the arithmetic — including the wizard's own context-budget calculation rather
// than a second copy of it — so a rung that cannot serve a tool says so for the
// same reason the backend would refuse it.
let _tierLadder = { default: 'balanced', ladder: [], tools: [], wizard_min_context_tokens: 16384 };

function _tier(name) {
    return (_tierLadder.ladder || []).find((t) => t.preset === name) || null;
}

const _TOOL_LABELS = {
    wizard: 'Node Wizard', adapter: 'ContainerAdapter', assistant: 'AI Assistant',
    support: 'Support Chat', repair: 'Build Repair',
};

function _esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Hardware floor. The CARD, not the model — a 4 GB estimate does not fit on a
// 4 GB card once the driver, the KV cache at full context and allocator
// fragmentation come out of the same budget, and cards sold as "8 GB" rarely
// expose 8 GB to a process. Showing the estimate as the requirement is how
// someone buys hardware that OOMs on first load, so both are shown and labelled.
function _tierFloorText(t) {
    if (t.compute_target === 'cpu') {
        return `No GPU · ${t.min_ram_gb || '?'} GB RAM minimum`;
    }
    const floor = t.vram_floor_gb || t.vram_est_gb || 0;
    let txt = `Needs a ${floor} GB GPU`;
    if (t.vram_est_gb && t.vram_est_gb < floor) {
        txt += ` (model itself ~${t.vram_est_gb} GB; the rest is driver, KV cache and headroom)`;
    }
    return txt;
}

// §5.1/§5.2 — reasoning, per rung.
//
// The ladder used to imply one reasoner everywhere, because there was one: a
// single hardcoded model appended to every vLLM rung regardless of the hardware
// that rung was named for. Now a rung declares its own, or declares none — and
// none is an ANSWER on small hardware, so it is stated rather than left to be
// inferred from a reasoner that simply is not mentioned.
function _reasoningLine(t) {
    const r = t.reasoning;
    if (!r) return '';
    if (r.available) {
        return `<div class="mt-1"><span class="text-success">Reasoning: ` +
               `${_esc(r.model || 'yes')}</span></div>`;
    }
    return `<div class="mt-1"><span class="text-warning font-medium">` +
           `No separate reasoning model on this tier</span>` +
           (r.note ? `<div class="text-success/80">${_esc(r.note)}</div>` : '') +
           `</div>`;
}

// §5.3/§5.4/§5.5 — what this rung needs, and whether this fleet has it.
//
// The verdict comes from the server, computed by the SAME mechanism that
// reports why a managed-AI stack could not be placed. Calculating it again here
// would eventually disagree with placement, and the disagreement shows up as a
// rung that reads selectable and then fails — the exact failure being fixed.
// §5.8 — what a rung actually provisions: each stack, its model, its demand and
// how it is served. A single verdict tells an operator whether the tier fits;
// it does not tell them WHAT they are getting for it.
function _rungRoles(t) {
    const roles = (t.placement && t.placement.roles) || [];
    if (!roles.length) return '';
    const rows = roles.map((r) =>
        `<div class="pl-4">${_esc(r.role)}: <span class="font-medium">${_esc(r.model || '?')}</span>` +
        ` &middot; ${_esc(String(r.vram_floor_gb))} GB &middot; ${_esc(r.serving_stack || '')}</div>`
    ).join('');
    return `<div class="mt-1">Provisions:</div>${rows}`;
}

function _hardwareLine(t) {
    const req = t.hardware_requirement;
    const pl = t.placement;
    if (!req && !pl) return '';

    const head = req
        ? `<div class="mt-1">Requires: <span class="font-medium">${_esc(req)}</span>` +
          (t.total_demand_gb ? ` <span class="text-success/80">` +
              `(${_esc(String(t.total_demand_gb))} GB total` +
              `${t.shares_one_card ? ', both models share one card' : ''})</span>` : '') +
          `</div>`
        : '';

    if (!pl) return head;
    if (pl.placeable) {
        return head + _rungRoles(t) +
               `<div class="text-success">&check; This fleet can host it</div>`;
    }
    // §5.9 — two failures with opposite fixes. "No card here is big enough,
    // ever" means buy hardware; "big enough but busy" means free some. Reported
    // as one flat "unplaceable" they send an operator to buy a card they
    // already own.
    const busy = pl.capacity_is_temporary === true;
    const headline = busy
        ? '&times; Not enough FREE capacity right now'
        : '&times; No accelerator here is large enough for this tier';
    const busyNote = busy
        ? '<div class="pl-4 text-warning">The hardware is big enough — it is ' +
          'currently in use. Free capacity, or stop what is holding it.</div>'
        : '';

    const instead = pl.fits_instead
        ? `<div class="pl-4">The <span class="font-medium">${_esc(pl.fits_instead.preset)}</span> ` +
          `tier ${_esc(pl.fits_instead.reason)} &mdash; select it above.</div>`
        : '';
    return head + _rungRoles(t) +
        `<div class="${busy ? 'text-warning' : 'text-destructive'} font-medium">${headline}</div>` +
        busyNote +
        (pl.reason ? `<div class="pl-4 ${busy ? 'text-warning' : 'text-destructive'}">${_esc(pl.reason)}</div>` : '') +
        instead;
}

function _updatePresetSummary(form, config) {
    const help = form.querySelector('[data-field-help="local_preset_summary"]');
    if (!help) return;
    const name = (config && config.local_preset) || _tierLadder.default;
    const t = _tier(name);
    if (!t) { help.textContent = ''; return; }

    const bits = [
        _tierFloorText(t),
        `${Number(t.context_tokens || 0).toLocaleString()} token context`,
        `${Number(t.input_budget_tokens || 0).toLocaleString()} usable for input`,
        `${t.serving_stack}`,
        `~${t.concurrent_users} concurrent`,
    ];

    // Vision is a capability difference, not a speed one, and it has to be
    // visible BEFORE selection rather than discovered when an image is ignored.
    const vision = t.vision
        ? '<span class="text-success">Vision: images supported</span>'
        : '<span class="text-warning font-medium">Text only &mdash; image inputs are ignored</span>';

    // Per-tool verdicts, with the reason. "Off" and "on but slow" are different
    // facts and an operator needs both.
    const toolRows = (_tierLadder.tools || []).map((tool) => {
        const v = (t.tools || {})[tool] || { available: true, warnings: [] };
        const label = _TOOL_LABELS[tool] || tool;
        const mark = v.available
            ? '<span class="text-success">&check;</span>'
            : '<span class="text-destructive font-semibold">&times;</span>';
        const why = (v.warnings || []).map((w) => `<div class="pl-4 text-success/80">${_esc(w)}</div>`).join('');
        const name = v.available ? _esc(label) : `<span class="text-destructive">${_esc(label)}</span>`;
        return `<div>${mark} ${name}</div>${why}`;
    }).join('');

    help.innerHTML =
        `<div>${_esc(t.label)}</div>` +
        `<div class="mt-1">${bits.map(_esc).join(' &middot; ')}</div>` +
        `<div class="mt-1">${vision}</div>` +
        _reasoningLine(t) +
        _hardwareLine(t) +
        `<div class="mt-2 space-y-0.5">${toolRows}</div>` +
        `<div data-field-help="accelerator_support" class="mt-2"></div>`;

    _renderAcceleratorSupport(form, name);
}

// ── Managed-AI state (§7) ────────────────────────────────────────────────────
//
// GE.AIC.6(a): the per-stack provisioning/placement/pin-warm CARD LIST that
// used to live here moved to the Stacks page (persistent-stacks.js already
// has its own host-pin UI). What's left in this file is just enough to keep
// `_managedAiState` populated for the master bar's own status text below.
//
// The managed-AI path in effect, so the local-mode banner can describe the path
// this deployment actually uses instead of assuming the compose one.
let _managedAiState = null;

// The access token, from the one place the app actually keeps it.
//
// This file had TWO conventions: `sessionStorage.accessToken` (what api.js
// writes at login) and `localStorage.access_token` (which nothing writes). The
// second is always null, so every call using it went out as `Bearer null`, came
// back 401, and hit an `if (!r.ok) return` — failing silently by design, since
// these panels are meant to stay hidden for users who cannot see them.
//
// The result: the accelerator-support panel, the managed-AI stacks panel and
// the placement/host-pin panel rendered for nobody, and looked merely absent
// rather than broken. Reading the token in one place is what stops that
// recurring.
function _authToken() {
    return sessionStorage.getItem('accessToken');
}


/** system-managed-ai — one authed JSON call. The master bar talks to three
 *  endpoints and every one of them needs the same header; writing it out
 *  three times is how the Bearer-null bug got in the first time. */
async function _masterFetch(path, init) {
    const base = window.APP_CONFIG?.API_URL || '';
    const resp = await fetch(`${base}${path}`, {
        ...(init || {}),
        headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${_authToken()}`,
            ...((init || {}).headers || {}),
        },
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        const d = body.detail;
        const msg = (d && typeof d === 'object') ? (d.message || JSON.stringify(d)) : d;
        throw new Error(msg || `HTTP ${resp.status}`);
    }
    return body;
}

// GE.AIC.6(a) (2026-09-29): the "Managed AI stacks" list box (per-stack
// pin-warm + host-pin cards) was removed from AI Config — the Stacks page
// already covers stacks, including host pins (persistent-stacks.js). This
// function is now fetch-only: it keeps `_managedAiState` populated (the
// master bar's own status text — the ai-ollama-banner / read-only banner
// below — still surfaces "stack path on/off + tier" from it) without
// rendering a box that no longer exists in the DOM. The Placement &
// recovery report (reconciler/rebalancer detail, `_renderStackPlacement`
// below) MOVED to the Persistent Stacks page's existing Recovery panel
// (`persistent-stacks.js:_psLoadHealth`), which already polled the same
// `/stacks/placement` endpoint for its reconciler/drift report and now also
// renders the rebalancer worker-score breakdown that used to live here.
async function _refreshManagedAiState() {
    // CE (GT.D5): the paid Managed-AI stack view is not served here (402 pro_feature);
    // keep _managedAiState null (the banner falls back to the compose path), request nothing.
    _applyOllamaGating();
}

// ── ai-config-stack-binding §7.1 — the "AI stacks" section ──────────────────
//
// The box GE.AIC.6(a) emptied is now the AI stacks section: every LLM stack,
// its model, whether it is running / sleeping / down, a LIVE health dot
// (is its server answering right now), a link to that stack's Persistent
// Stacks row, and the slots bound to it ("used by", design D1).
//
// Data: GET /platform/ai-config/ai-stacks (rows + live verdict, schemas.AiStackRow)
// and GET /platform/ai-config/bindings (the nine-slot read — used here for
// the footer's "slots nobody bound", which ai-stacks deliberately omits).
// Both are platform-admin only, so a tenant user simply gets nothing: the
// 403 fails the fetch, and the section stays hidden rather than showing an
// error to someone who was never meant to see it.
//
// A FAILED call (404 before the API restart that ships the endpoint, 403,
// dead token) must never blank the section: the last good render is kept,
// and if nothing ever rendered, a one-line inline notice replaces it.
let _aiStacksLast = null;
// CE: the AI stacks DATA loads (slot pickers read _aiStacksLast) but the section
// stays hidden (owner: nothing new visible on CE). Set by loadAIConfig.
let _aiStacksKeepHidden = false;

// Stack status -> the three words the page uses. `stopped` is deliberately
// "sleeping", not "down": a stack the operator parked to free the card is
// asleep on its own, while `failed`/`unreachable` is something broken.
function _aiStackStatusWord(status) {
    const s = String(status || '').toLowerCase();
    if (s === 'running') return 'running';
    if (['deploying', 'migrating', 'created', 'starting', 'waking'].includes(s)) return 'starting';
    if (['stopped', 'torn_down', 'teardown', 'asleep', 'idle'].includes(s)) return 'sleeping';
    if (['failed', 'error', 'unreachable', 'unplaceable', 'down'].includes(s)) return 'down';
    return s;
}

// The dot colour classes are ones CORE/Dashboard/tailwind.css already ships.
function _aiStackDot(liveHealthy) {
    if (liveHealthy === true) return 'bg-green-500';
    if (liveHealthy === false) return 'bg-red-500';
    return 'bg-gray-300';
}

function _aiStackSlotChip(res) {
    const slot = _esc(res.slot || '');
    const outcome = String(res.outcome || '');
    if (outcome === 'ready') {
        return `<span class="px-2 py-0.5 rounded-full text-xs bg-green-100 text-green-700">${slot}</span>`;
    }
    const cause = _esc(res.message || outcome || '');
    return `<span class="px-2 py-0.5 rounded-full text-xs bg-amber-100 text-gray-800">${slot}: ${cause}</span>`;
}

function _renderAiStacksSection(data) {
    const box = document.getElementById('ai-stacks-section');
    if (!box) return;
    const stacks = Array.isArray(data?.stacks) ? data.stacks : [];
    const parts = [`<h3 class="text-lg font-medium text-gray-800">AI stacks</h3>`];
    if (!stacks.length) {
        parts.push(`<p class="text-sm text-gray-500">No stack here serves an AI model yet.</p>`);
    }
    stacks.forEach((st) => {
        const id = st.stack_id;
        const statusWord = _aiStackStatusWord(st.status);
        const model = _esc(st.model || (st.served_models || [])[0] || 'no model reported');
        const chips = (st.used_by || []).map(_aiStackSlotChip).join(' ');
        const retired = st.retired_binding_count || 0;
        parts.push(
            `<div class="flex items-center gap-2 text-sm">` +
            `<span class="inline-block w-2 h-2 rounded-full ${_aiStackDot(st.live_healthy)}" ` +
            `title="live health: ${st.live_healthy === true ? 'answering' : st.live_healthy === false ? 'not answering' : 'unknown'}"></span> ` +
            `<span class="font-medium text-gray-800">${_esc(st.name || '')}</span> ` +
            `<span class="text-gray-500">${statusWord} · ${model} · kind ${_esc(st.kind || '')}</span>` +
            `</div>` +
            `<div class="text-xs text-gray-500">` +
            `<a href="#" data-ps-stack-id="${_esc(String(id ?? ''))}" class="underline text-primary">Persistent Stacks row</a> ` +
            `${chips}` +
            (retired ? ` · ${retired} retired binding(s)` : '') +
            (st.live_error ? ` · <span class="text-red-600">${_esc(st.live_error)}</span>` : '') +
            `</div>`);
    });
    box.innerHTML = parts.join('');
    if (!_aiStacksKeepHidden) box.classList.remove('hidden');
}

// One-line inline notice for a failed read. Deliberately one line, and
// deliberately NOT a blank: the section either shows the stacks it has or
// says why it cannot, so a page that looks "empty" is never a page that is
// silently broken. The last good render stays in the DOM behind the notice
// when there is one (the notice replaces the box only if nothing rendered).
function _aiStacksNotice(detail) {
    const box = document.getElementById('ai-stacks-section');
    if (!box) return;
    const had = _aiStacksLast !== null;
    if (!had && !_aiStacksKeepHidden) box.classList.remove('hidden');
    const line = document.createElement('p');
    line.className = 'text-xs text-gray-500';
    line.textContent = `AI stacks list unavailable: ${detail}`;
    if (had) {
        // Keep the old rendering, replace only the notice node.
        const prev = box.querySelector('[data-ai-stacks-notice]');
        if (prev) { prev.textContent = line.textContent; return; }
        line.setAttribute('data-ai-stacks-notice', '1');
        box.appendChild(line);
    } else {
        line.setAttribute('data-ai-stacks-notice', '1');
        box.innerHTML = line.outerHTML;
    }
}

async function _loadAiStacksSection() {
    // Both endpoints are platform-admin only (same gate as
    // /platform/managed-ai/setting). A tenant user gets NOTHING here — not a
    // notice — because "unavailable" would read as broken to someone who was
    // never meant to see the section.
    if (!(currentUser && currentUser.is_platform_admin)) return;
    const token = _authToken();
    if (!token) return;
    const headers = { Authorization: `Bearer ${token}` };
    // D75 (): the ai-stacks read feeds the slot pickers
    // (`_aiStacksLast`). A failed read used to give up on the FIRST try, so
    // the pickers stayed empty and the Easy table said 'No stacks yet' even
    // though stacks exist. Now: 3 attempts, 1.5 s apart. A failed read is
    // never cached as an empty list (`_aiStacksLast` is only set on success),
    // and the hint distinguishes 'no stacks' from 'could not load'.
    let stacks = null, bindings = null, detail = '';
    try {
        const br = await fetch(`${window.APP_CONFIG?.API_URL || ''}/platform/ai-config/bindings`,
            { headers });
        if (br.ok) bindings = await br.json();
    } catch (e) {
        detail = (e && e.message) || String(e);
    }
    for (let attempt = 1; attempt <= 3; attempt += 1) {
        try {
            const sr = await fetch(`${window.APP_CONFIG?.API_URL || ''}/platform/ai-config/ai-stacks`,
                { headers });
            if (sr.ok) { stacks = await sr.json(); break; }
            detail = `HTTP ${sr.status}`;
        } catch (e) {
            detail = (e && e.message) || String(e);
        }
        if (stacks) break;
        if (attempt < 3) {
            _aiStacksNotice('could not load your stacks: retrying');
            await new Promise(r => setTimeout(r, 1500));
        }
    }
    if (!stacks) {
        _aiStacksNotice(detail || 'the AI stacks endpoint did not answer');
        return;
    }
    _aiStacksLast = stacks;
    // The nine-slot read (GET /platform/ai-config/bindings) feeds the
    // footer only: ai-stacks already carries per-stack "used by", while
    // bindings says which slots have NO row at all — "nobody bound this
    // slot" is a different sentence from "bound and stopped".
    _renderAiStacksSection({ stacks });
    // Fold the unbound-slot footer back in (the render above skipped it to
    // keep the row loop readable).
    const box = document.getElementById('ai-stacks-section');
    if (box && bindings) {
        const unbound = (bindings.unbound_slots || []).map((s) => _esc(s)).join(', ');
        const foot = document.createElement('p');
        foot.className = 'text-xs text-gray-500';
        foot.textContent = unbound
            ? `Slots nobody bound: ${unbound}.`
            : 'Every AI slot has a binding.';
        box.appendChild(foot);
    }
}

// The "Persistent Stacks row" link: same deep link library.js uses to open
// one stack's row (?tab=stacks&stack=<id>#system + popstate). replaceState
// fires no popstate of its own, so it is dispatched — `new Event('popstate')`
// is the standard constructor (ui.js listens on popstate and routes on
// window.location.hash).
function _gotoPersistentStackRow(stackId) {
    const params = new URLSearchParams();
    params.set('tab', 'stacks');
    if (stackId != null && stackId !== '') params.set('stack', String(stackId));
    window.history.replaceState(null, '',
        `${window.location.pathname}?${params.toString()}#system`);
    window.dispatchEvent(new Event('popstate'));
}

function _wireAiStacksLinks() {
    const box = document.getElementById('ai-stacks-section');
    if (!box || box.dataset.aiStacksWired) return;
    box.dataset.aiStacksWired = '1';
    box.addEventListener('click', (e) => {
        const link = e.target.closest('a[data-ps-stack-id]');
        if (!link) return;
        e.preventDefault();
        _gotoPersistentStackRow(link.dataset.psStackId);
    });
}

// ---------------------------------------------------------------------------
// ai-config-stack-binding §7.2 — "Who uses what".
//
// One row per slot (the six chat slots: wizard, adapter, assistant, support,
// repair, critic). Each row is: a dropdown over (AI stacks ∪ external
// endpoints ∪ "Let MOD choose"), the slot's health dot, and the resolver's
// cause sentence when the slot's outcome is not `ready` — the SAME typed
// sentence the resolver gives a feature ("stack X is stopped", "nothing
// answers at host:port", "nobody picked"), never a blank.
//
// Reads (all platform-admin, same gate as §7.1):
//   GET /platform/ai-config/bindings   → the row's outcome + cause
//   GET /platform/ai-config/ai-stacks  → the dropdown's stack options + live
//                                         health (reuses §7.1's `_aiStacksLast`,
//                                         so one read feeds both sections)
//   _aiFormState (already loaded by loadAIConfig from /ai-config/all)
//                                        → the dropdown's EXTERNAL endpoints:
//                                          every base_url/model pair the
//                                          operator already configured. No new
//                                          fetch, and no URL is invented here.
//
// Writes go through the existing bindings API — PUT /platform/ai-config/
// bindings/{slot} with AiBindingUpdate (schemas.py:3880): target_kind
// 'stack'|'external'|'auto', stack_id for stacks, base_url + model for
// externals, priority 0 (the primary position). No key is ever sent: the
// endpoint takes a Vault PATH (`key_ref`), never a secret, and only echoes
// `key_ref_has_key`. The PUT's response carries the slot's new `resolution`,
// so the row redraws from the write itself — the operator sees the effect of
// their pick, not a guess.
//
// The ten per-tool tabs stay (they still hold the per-tool knobs), but their
// execution-mode dropdowns are gone: the template ships none, and
// _hideLegacyExecutionModeSelects() hides one if an older build left it in the
// DOM. Bindings live in ONE place — this table.
// ---------------------------------------------------------------------------

const _AI_SLOT_ROWS = ['wizard', 'adapter', 'assistant', 'support', 'repair', 'critic'];
// §7.2b (owner BUG.B57/B58/B59) — the three embedding slots, under their own
// "Embeddings" sub-heading. Same nine names models.py's
// `ck_ai_slot_binding_slot` allows (services/ai_slot_resolver.py EMBEDDING_SLOTS).
const _AI_EMBED_ROWS = ['wizard_learning_embed', 'assistant_user_rag_embed',
                        'assistant_knowledge_embed'];
const _AI_EMBED_LABELS = {
    wizard_learning_embed: 'Wizard learning (embeddings)',
    assistant_user_rag_embed: 'Assistant — your documents (embeddings)',
    assistant_knowledge_embed: 'Assistant knowledge base (embeddings)',
};
const _AI_SLOT_LABELS = {
    wizard: 'Node Wizard',
    adapter: 'Container Adapter',
    assistant: 'AI Assistant',
    support: 'Support Chat',
    repair: 'Build Repair',
    critic: 'Wizard code review (critic)',
    ..._AI_EMBED_LABELS,
};

// §7.2b (owner BUG.B56) — what the operator picked at each CHAIN POSITION of a
// slot: `_aiSlotChosenAt[slot][priority] = 'auto' | 'stack:<id>' | 'ext:<n>'`.
// The GET bindings read reports the whole chain (`attempts`, priority order)
// but only names the EFFECTIVE target, so the page remembers its own writes to
// label the fallbacks; a position it never wrote is labelled by the resolver's
// own sentence for that attempt instead of a guess.
const _aiSlotChosenAt = {};

// The dropdown's chosen value per slot, remembered so a re-render (a save, or
// the next poll) restores the operator's pick instead of snapping it back to
// "Choose…". `auto` = Let MOD choose, `stack:<id>`, `ext:<n>` into
// _aiSlotExternals.
const _aiSlotChosen = {};
let _aiSlotExternals = [];

// The dot for a SLOT (not a stack): green when the slot answers, red when it
// is bound and broken, grey when nobody ever bound it (unknown, not broken).
function _aiSlotDot(outcome) {
    if (outcome === 'ready') return 'bg-green-500';
    if (outcome === 'no_binding') return 'bg-gray-300';
    return 'bg-red-500';
}

// The external endpoints offered in every row, derived from the per-tool
// configs loadAIConfig already fetched (`_aiFormState`): every base_url the
// operator configured, de-duplicated, with the tools that use it named.
// Deliberately NOT a new fetch and never an invented URL — an endpoint the
// page cannot name is an endpoint the page cannot offer.
function _aiSlotCollectExternals() {
    const byUrl = new Map();
    Object.entries(_aiFormState || {}).forEach(([tool, cfg]) => {
        const url = String(cfg.base_url || '').trim();
        if (!url) return;
        const rec = byUrl.get(url) || { url, models: [], tools: [] };
        if (cfg.model && !rec.models.includes(cfg.model)) rec.models.push(cfg.model);
        if (tool && !rec.tools.includes(tool)) rec.tools.push(tool);
        byUrl.set(url, rec);
    });
    return [...byUrl.values()];
}

// The option list for ONE chain position of one slot. `capability` is the
// API's own kind read (AiStackRow.kind, and the same value the bindings PUT
// derives server-side via _slot_capability), so an embedding slot only ever
// offers embedding stacks — a chat stack cannot be picked into it and then
// 422 on save. External endpoints carry no capability in the API, so they are
// offered as configured and labelled as such.
//
// D76 (owner Tim, PIN7 sitting 4): the rows must offer the stacks the user
// OWNS, not only the stacks §7.1 calls "LLM stacks". §7.1 deliberately drops
// a stack that serves no model and is bound by nothing
// (routers/ai_config.py:3318 `if not mine and not served: continue`), so a
// stack that exists on the Stacks tab but is not bound yet never reached the
// picker: the row said "Use a stack…" over an empty dropdown ("it does not
// poll the stacks"). The picker now reads the SAME stacks the Advanced tab's
// Stack pickers read (GET /platform/ai-config/stack-options -> `_stackOptions`)
// merged with the §7.1 rows, keyed by stack_id. A stack that cannot serve a
// model yet stays selectable exactly like a stopped stack does, and its save
// answers with the API's own sentence naming why (ai_stack_provider
// `_pick_served_model`: "does not declare a served model — it can't be used
// as a provider yet"). No new endpoint, no filter of our own: the API's kind
// read still decides which slot a stack can serve.
//
// The `typeof` guard on `_stackOptions` is deliberate: the shipped file
// declares it, but the extracted-code tests run this function against a stub
// env that carries none, and an undeclared bare identifier would throw there.
function _aiSlotFillOptions(sel, capability) {
    const byId = new Map();
    (Array.isArray(_aiStacksLast?.stacks) ? _aiStacksLast.stacks : [])
        .forEach((st) => { if (st?.stack_id != null) byId.set(Number(st.stack_id), st); });
    (typeof _stackOptions === 'undefined' ? [] : _stackOptions)
        .forEach((o) => {
            if (o?.stack_id == null) return;
            const sid = Number(o.stack_id);
            if (!byId.has(sid)) byId.set(sid, o);
        });
    const stacks = [...byId.values()].filter((st) => st?.kind === capability);
    const ph = document.createElement('option');
    ph.value = '';
    ph.textContent = 'Choose…';
    sel.appendChild(ph);
    if (_paidEditionKnown) {
        const auto = document.createElement('option');
        auto.value = 'auto';
        auto.textContent = 'Let MOD choose';
        sel.appendChild(auto);
    }
    stacks.forEach((st) => {
        const opt = document.createElement('option');
        opt.value = `stack:${st.stack_id}`;
        opt.textContent = `${st.name || ''} — ${_aiStackStatusWord(st.status)}` +
            ((st.served_models || []).length > 1 ? ` (${(st.served_models || []).length} models)` : '');
        sel.appendChild(opt);
    });
    _aiSlotExternals.forEach((ex, i) => {
        const opt = document.createElement('option');
        opt.value = `ext:${i}`;
        opt.textContent = `${ex.url}${ex.models.length ? ` — ${ex.models.join(', ')}` : ''}` +
            `${ex.tools.length ? ` (used by ${ex.tools.join(', ')})` : ''}`;
        sel.appendChild(opt);
    });
    return stacks.length;
}

// The label for a chain position the operator picked (`auto` / `stack:<id>` /
// `ext:<n>`) — never for a position the page never wrote, which is labelled
// by the resolver's own sentence instead of a guess.
function _aiSlotValueLabel(value) {
    if (!value || value === 'auto') return 'Let MOD choose';
    if (value.startsWith('stack:')) {
        // D76: the picker also offers the stacks that only the stack-options
        // read knows (unbound, serving nothing yet), so a chain position the
        // operator picked there labels by name, not as `stack #7`.
        const st = (Array.isArray(_aiStacksLast?.stacks) ? _aiStacksLast.stacks : [])
            .find((s) => String(s?.stack_id) === value.slice(6))
            || (typeof _stackOptions === 'undefined' ? [] : _stackOptions)
                .find((s) => String(s?.stack_id) === value.slice(6));
        return st ? `${st.name || ''} — ${_aiStackStatusWord(st.status)}` : `stack #${value.slice(6)}`;
    }
    if (value.startsWith('ext:')) {
        const ex = _aiSlotExternals[Number(value.slice(4))];
        return ex ? ex.url : 'external endpoint';
    }
    return value;
}

// One chain position: `#0 primary`, `#1 fallback`, ... each with the target
// label, the resolver's own sentence for THAT attempt (owner BUG.B56: every
// failure names its own cause), and up / down / remove controls.
// Remove = retire (PUT enabled=false, the API stamps retired_at and never
// hard-deletes — ISO 27001 soft delete).
function _aiSlotChainLine(slot, res, attempts) {
    if (!attempts.length) return null;
    const wrap = document.createElement('div');
    wrap.className = 'flex flex-wrap items-center gap-1 text-xs mt-2';
    wrap.dataset.role = 'chain';
    attempts.forEach((a) => {
        const p = Number(a?.priority ?? 0);
        const mine = _aiSlotChosenAt[slot]?.[p];
        const isTarget = res?.target && Number(res.target.priority ?? 0) === p;
        const label = mine ? _aiSlotValueLabel(mine)
            : isTarget ? (res.target.stack_name || res.target.model || res.target.base_url || 'bound target')
            : 'a binding this page never wrote';
        const outcome = String(a?.outcome || '');
        const line = document.createElement('div');
        line.className = 'flex items-center gap-2 text-xs';
        const txt = document.createElement('span');
        // The status line under the row already prints res.message in full; when
        // this attempt carries that SAME sentence, repeating it here makes the
        // page ask the same question twice (owner report, Settings > AI
        // Configuration). Say it short instead; distinct fallback causes keep
        // their own text.
        const attemptMsg = String(a?.message || '');
        const echoStatus = attemptMsg !== '' && attemptMsg === String(res?.message || '') && outcome !== 'ready';
        txt.textContent = `#${p} ${p === 0 ? 'primary' : 'fallback'} — ${label} · ` +
            (outcome === 'ready' ? 'answering'
                : echoStatus ? 'not answering'
                : (attemptMsg || outcome || 'unknown'));
        line.appendChild(txt);
        ['up', 'down', 'remove'].forEach((act) => {
            const b = document.createElement('button');
            b.className = 'px-1 py-1 rounded-md text-gray-500 border border-gray-300';
            b.dataset.act = act;
            b.dataset.slot = slot;
            b.dataset.priority = String(p);
            b.textContent = act === 'up' ? '↑' : act === 'down' ? '↓' : 'remove';
            b.addEventListener('click', () => { _aiSlotChainAct(slot, p, act); });
            line.appendChild(b);
        });
        wrap.appendChild(line);
    });
    return wrap;
}

// The "Add fallback" button: it appends the next free chain position
// (max priority + 1, the API allows 0..15) and reveals a dropdown of the SAME
// options as the row's own. Choosing writes PUT .../bindings/{slot} with that
// priority — a NEW row, which is exactly how the API adds a second row.
function _aiSlotAddFallbackButton(slot) {
    const b = document.createElement('button');
    b.className = 'px-2 py-1 rounded-md text-xs text-gray-700 border border-gray-300';
    b.dataset.role = 'add-fallback';
    b.dataset.slot = slot;
    b.textContent = 'Add fallback';
    b.addEventListener('click', () => {
        const box = document.getElementById('ai-slots-table');
        if (!box) return;
        const row = box.querySelector(`[data-slot="${slot}"]`);
        if (!row) return;
        const sel = row.querySelector('select[data-field="slot_add_fallback"]');
        if (!sel) return;
        sel.value = '';
        sel.classList.remove('hidden');
    });
    return b;
}

function _renderAiSlotsTable(bindings) {
    const box = document.getElementById('ai-slots-table');
    if (!box) return;
    const rows = Array.isArray(bindings?.rows) ? bindings.rows : [];
    const bySlot = {};
    rows.forEach((r) => { if (r?.slot) bySlot[r.slot] = r; });

    _aiSlotExternals = _aiSlotCollectExternals();

    const head = document.createElement('h3');
    head.className = 'text-lg font-medium text-gray-800';
    head.textContent = 'Who uses what';
    box.innerHTML = '';
    box.appendChild(head);
    const intro = document.createElement('p');
    intro.className = 'text-xs text-gray-500';
    intro.textContent =
        'One row per AI slot. Pick the stack, the external endpoint, or let MOD choose; saving writes the binding.';
    box.appendChild(intro);

    const renderOne = (slot, capability) => {
        const res = bySlot[slot];
        const outcome = res ? String(res.outcome || '') : 'no_binding';
        const row = document.createElement('div');
        row.className = 'rounded-lg border border-gray-200 bg-white p-3 space-y-1 mb-3';
        row.dataset.slot = slot;

        const top = document.createElement('div');
        top.className = 'flex items-center gap-2 text-sm';
        const dot = document.createElement('span');
        dot.className = `inline-block w-2 h-2 rounded-full ${_aiSlotDot(outcome)}`;
        dot.title = outcome === 'ready' ? 'answering'
            : outcome === 'no_binding' ? 'nobody bound this slot' : 'bound, not answering';
        const name = document.createElement('span');
        name.className = 'font-medium text-gray-800';
        name.textContent = _AI_SLOT_LABELS[slot] || slot;
        top.appendChild(dot);
        top.appendChild(name);
        row.appendChild(top);

        // D75 (owner Tim, Gate 7 sitting 3): in Easy mode a row must READ as a
        // way to hook one of the user's own stacks in, not as a bare dropdown.
        // The picker itself is unchanged — same options (`_aiStacksLast`, the
        // §7.1 read) and the same save (`_aiSlotSave` -> PUT
        // /platform/ai-config/bindings/{slot}) the Advanced tab's forms use —
        // it only gains a label, and a line saying what to do when no stack
        // serves this slot yet.
        const pickLabel = document.createElement('div');
        pickLabel.className = 'text-xs text-gray-500';
        pickLabel.dataset.role = 'slot-picker-label';
        pickLabel.textContent = 'Use a stack…';
        row.appendChild(pickLabel);

        const sel = document.createElement('select');
        sel.dataset.field = 'slot_binding';
        sel.dataset.slot = slot;
        sel.className = 'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm';
        const stackOptions = _aiSlotFillOptions(sel, capability);
        const chosen = _aiSlotChosen[slot];
        if (chosen && [...sel.options].some((o) => o.value === chosen)) sel.value = chosen;
        row.appendChild(sel);

        if (!stackOptions) {
            const none = document.createElement('div');
            none.className = 'text-xs text-gray-500';
            none.dataset.role = 'slot-no-stacks';
            // D75 (): 'No stacks yet' is only honest when the stacks
            // read SUCCEEDED and answered zero. A failed read (`_aiStacksLast`
            // still null) says it could not load, so a user with stacks is
            // never told to create one.
            none.textContent =
                _aiStacksLast === null
                    ? 'Could not load your stacks. Reload to try again.'
                    : 'No stacks yet: create one on the Stacks tab, then pick it here.';
            row.appendChild(none);
        }

        const attempts = (Array.isArray(res?.attempts) ? res.attempts : [])
            .slice().sort((x, y) => Number(x?.priority ?? 0) - Number(y?.priority ?? 0));
        const chain = _aiSlotChainLine(slot, res, attempts);
        if (chain) row.appendChild(chain);

        // "Add fallback" + its dropdown, at the next free chain position.
        const maxP = attempts.reduce((m, a) => Math.max(m, Number(a?.priority ?? 0)), -1);
        const nextP = Math.max(1, maxP + 1);
        if (nextP <= 15) {
            const addWrap = document.createElement('div');
            addWrap.className = 'flex items-center gap-2 mt-2';
            addWrap.appendChild(_aiSlotAddFallbackButton(slot));
            const addSel = document.createElement('select');
            addSel.dataset.field = 'slot_add_fallback';
            addSel.dataset.slot = slot;
            addSel.dataset.priority = String(nextP);
            addSel.className = 'hidden w-full border border-gray-300 rounded-lg px-3 py-2 text-sm';
            _aiSlotFillOptions(addSel, capability);
            addWrap.appendChild(addSel);
            row.appendChild(addWrap);
        }

        const foot = document.createElement('div');
        foot.className = 'text-xs text-gray-500';
        foot.dataset.role = 'slot-status';
        const cause = String(res?.message || '');
        if (outcome !== 'ready' && cause) {
            foot.innerHTML = `<span class="text-red-600">${_esc(cause)}</span>`;
        } else if (outcome === 'ready' && res?.target) {
            foot.textContent = `serving ${res.target.stack_name || res.target.model || res.target.base_url || ''}`;
        } else if (outcome === 'ready') {
            foot.textContent = 'answering';
        } else {
            foot.textContent = 'nobody bound this slot yet';
        }
        row.appendChild(foot);
        box.appendChild(row);
    };

    _AI_SLOT_ROWS.forEach((slot) => renderOne(slot, 'chat'));

    const embedHead = document.createElement('h4');
    embedHead.className = 'text-sm font-medium text-gray-700 mt-4 mb-2';
    embedHead.textContent = 'Embeddings';
    box.appendChild(embedHead);
    const embedNote = document.createElement('p');
    embedNote.className = 'text-xs text-gray-500';
    embedNote.textContent =
        'The three embedding slots pick an embedding model only — the API reports which stacks serve embeddings. ' +
        'External endpoints are offered as the operator configured them; the API does not report whether one serves embeddings. ' +
        'The bindings API has no knowledge-source field, so the assistant knowledge base itself is not selectable here — only the model that embeds it.';
    box.appendChild(embedNote);
    _AI_EMBED_ROWS.forEach((slot) => renderOne(slot, 'embedding'));

    box.classList.remove('hidden');
}

// One-line notice for a failed bindings read — same shape as §7.1's: the
// section either shows the rows it has or says why it cannot, never blank.
function _aiSlotsNotice(detail) {
    const box = document.getElementById('ai-slots-table');
    if (!box) return;
    const line = document.createElement('p');
    line.className = 'text-xs text-gray-500';
    line.textContent = `Who-uses-what unavailable: ${detail}`;
    const prev = box.querySelector('[data-ai-slots-notice]');
    if (prev) { prev.textContent = line.textContent; return; }
    line.setAttribute('data-ai-slots-notice', '1');
    box.appendChild(line);
    // Nothing ever rendered here — the box is still hidden, and a hidden box
    // means the notice would be invisible. Reveal it so the page says why.
    if (!box.querySelector('[data-slot]')) box.classList.remove('hidden');
}

async function _loadAiSlotsTable() {
    // Same gate as §7.1: the bindings read is platform-admin only, so a
    // tenant user gets NOTHING here rather than an "unavailable" notice.
    if (!(currentUser && currentUser.is_platform_admin)) return;
    const token = _authToken();
    if (!token) return;
    let bindings = null, detail = '';
    try {
        const br = await fetch(`${window.APP_CONFIG?.API_URL || ''}/platform/ai-config/bindings`,
            { headers: { Authorization: `Bearer ${token}` } });
        if (br.ok) bindings = await br.json();
        else detail = `HTTP ${br.status}`;
    } catch (e) {
        detail = (e && e.message) || String(e);
    }
    if (!bindings) {
        _aiSlotsNotice(detail || 'the bindings endpoint did not answer');
        return;
    }
    _renderAiSlotsTable(bindings);
}

// Saving a row writes the binding through the existing bindings API —
// PUT /platform/ai-config/bindings/{slot} (AiBindingUpdate). `auto` = Let MOD
// choose (target_kind 'auto'); `stack:<id>` names the stack; `ext:<n>` reuses
// the endpoint the operator already configured (base_url + model). No api_key
// and no key_ref is ever sent from here: the endpoint takes a Vault PATH, and
// the page never handles a secret.
async function _aiSlotSave(slot, value, priority = 0) {
    const body = { target_kind: 'auto', priority: Number(priority) };
    if (value && value !== 'auto') {
        if (value.startsWith('stack:')) {
            body.target_kind = 'stack';
            body.stack_id = Number(value.slice(6));
        } else if (value.startsWith('ext:')) {
            const ex = _aiSlotExternals[Number(value.slice(4))];
            if (!ex) return false;
            body.target_kind = 'external';
            body.base_url = ex.url;
            if (ex.models.length === 1) body.model = ex.models[0];
        }
    }
    try {
        const r = await _masterFetch(`/platform/ai-config/bindings/${encodeURIComponent(slot)}`,
            { method: 'PUT', body: JSON.stringify(body) });
        // The API patched (or wrote) the row at `priority` — remember it there,
        // so the chain line under this row labels THAT position with the pick
        // the operator made rather than the resolver's guess.
        if (r?.priority != null && r?.target_kind) {
            (_aiSlotChosenAt[slot] || (_aiSlotChosenAt[slot] = {}))[Number(r.priority)] = value;
        }
        // Redraw THIS row from the resolution the PUT returned, so the operator
        // sees the effect of their pick immediately (AiBindingResult.resolution).
        if (r?.resolution) _aiSlotApplyResolution(slot, r.resolution);
        return r || true;
    } catch (e) {
        _aiSlotsNotice((e && e.message) || String(e));
        return false;
    }
}

// §7.2b (owner BUG.B56) — the chain controls. The API has no "move" verb: a
// position is written by PUT .../bindings/{slot} with its `priority`, so
// `up`/`down` are the two writes that swap the neighbours (each position is
// patched in place). `remove` is the SOFT delete the endpoint already
// implements: `enabled=false` stamps `retired_at` and the row is NEVER
// hard-deleted (ISO 27001), which is also why reviving a position writes a
// NEW row rather than clearing a flag.
async function _aiSlotChainAct(slot, priority, act) {
    const mine = _aiSlotChosenAt[slot] || {};
    const p = Number(priority);
    if (act === 'remove') {
        // Retire: the endpoint needs no target_kind to retire an existing
        // live row, but a NEW row at an empty position must name one — so
        // only retire a position this page knows exists.
        if (mine[p] === undefined) {
            _aiSlotsNotice(`cannot retire position #${p} — this page never wrote it; pick it first`);
            return;
        }
        try {
            await _masterFetch(`/platform/ai-config/bindings/${encodeURIComponent(slot)}`,
                { method: 'PUT', body: JSON.stringify({ enabled: false, priority: p }) });
            delete mine[p];
            await _aiRefreshChain(slot);
        } catch (e) {
            _aiSlotsNotice((e && e.message) || String(e));
        }
        return;
    }
    const q = act === 'up' ? p - 1 : p + 1;
    if (q < 0 || q > 15) return;
    if (mine[p] === undefined || mine[q] === undefined) {
        _aiSlotsNotice(`cannot move position #${p} — a neighbour here was never written by this page; set it first`);
        return;
    }
    const a = mine[p], b = mine[q];
    // Two writes, each patching one position in place: a moves to q and b
    // moves to p. `_aiSlotSave` records each write at the position it wrote,
    // so a successful pair has left `mine` swapped by itself.
    const okA = await _aiSlotSave(slot, a, q);
    const okB = await _aiSlotSave(slot, b, p);
    if (okA && okB) await _aiRefreshChain(slot);
}

// Re-read the chain for ONE slot and redraw its chain line in place — never a
// full re-render, which would reset every other row's dropdown under the
// operator. The GET bindings response is the resolver's own truth (each
// attempt carries its own cause), so the chain shown is what the resolver
// will do, not what the page thinks it did.
async function _aiRefreshChain(slot) {
    const box = document.getElementById('ai-slots-table');
    if (!box) return;
    const row = box.querySelector(`[data-slot="${slot}"]`);
    if (!row) return;
    try {
        const r = await _masterFetch('/platform/ai-config/bindings', {});
        const res = (Array.isArray(r?.rows) ? r.rows : []).find((x) => x?.slot === slot);
        const attempts = (Array.isArray(res?.attempts) ? res.attempts : [])
            .slice().sort((x, y) => Number(x?.priority ?? 0) - Number(y?.priority ?? 0));
        const old = row.querySelector('[data-role="chain"]');
        if (old) old.parentNode.removeChild(old);
        const fresh = _aiSlotChainLine(slot, res, attempts);
        if (fresh) {
            const anchor = row.querySelector('[data-role="slot-status"]');
            if (anchor) anchor.parentNode.insertBefore(fresh, anchor);
            else row.appendChild(fresh);
        }
    } catch (e) {
        _aiSlotsNotice((e && e.message) || String(e));
    }
}

// Patch ONE row in place from an AiBindingResult.resolution — never a full
// re-render, which would reset every other row's dropdown under the operator.
function _aiSlotApplyResolution(slot, res) {
    const box = document.getElementById('ai-slots-table');
    if (!box) return;
    const row = box.querySelector(`[data-slot="${slot}"]`);
    if (!row) return;
    const outcome = String(res?.outcome || '');
    const dot = row.querySelector('span');
    if (dot) dot.className = `inline-block w-2 h-2 rounded-full ${_aiSlotDot(outcome)}`;
    const foot = row.querySelector('[data-role="slot-status"]');
    if (!foot) return;
    const cause = String(res?.message || '');
    if (outcome !== 'ready' && cause) {
        foot.innerHTML = `<span class="text-red-600">${_esc(cause)}</span>`;
    } else if (outcome === 'ready' && res?.target) {
        foot.textContent = `serving ${res.target.stack_name || res.target.model || res.target.base_url || ''}`;
    } else if (outcome === 'ready') {
        foot.textContent = 'answering';
    } else {
        foot.textContent = 'nobody bound this slot yet';
    }
}

// Wire the dropdowns once: a `change` remembers the pick and writes it; a
// `change` to "" (the Choose… placeholder) is a no-op, not a retirement —
// retiring a binding is an explicit action, never a dropdown left open.
function _wireAiSlotsTable() {
    const box = document.getElementById('ai-slots-table');
    if (!box || box.dataset.aiSlotsWired) return;
    box.dataset.aiSlotsWired = '1';
    box.querySelectorAll('select[data-field="slot_binding"]').forEach((sel) => {
        sel.addEventListener('change', async (e) => {
            const slot = e.target.dataset.slot;
            if (!slot) return;
            const v = e.target.value;
            if (!v) return;
            _aiSlotChosen[slot] = v;
            await _aiSlotSave(slot, v, 0);
        });
    });
    // §7.2b — the "Add fallback" dropdown writes the position it carries
    // (data-priority), which is the API's own way of adding a 2nd row: PUT
    // with that priority creates a NEW row at (slot, 'platform', priority).
    box.querySelectorAll('select[data-field="slot_add_fallback"]').forEach((sel) => {
        sel.addEventListener('change', async (e) => {
            const slot = e.target.dataset.slot;
            const p = Number(e.target.dataset.priority);
            if (!slot || !Number.isFinite(p)) return;
            const v = e.target.value;
            if (!v) return;
            if (await _aiSlotSave(slot, v, p)) await _aiRefreshChain(slot);
        });
    });
}

// BUG.B59c (owner Q-B59=A) — the assistant's knowledge embedding is a PICKER
// over knowledge sources (MOD Docs / tenant files / uploads), each with its own
// embedding model. Same conventions as the who-uses-what rows directly above:
// capability-filtered options (an embedding slot only ever offers embedding
// stacks, so a chat stack can never be picked and then 422 on save — and when
// the API does refuse, its own detail sentence is shown inline, never swallowed
// by a generic "save failed"), `key_ref` is never sent from here (the endpoint
// takes a Vault PATH and this page never handles a secret), and "Turn off" is
// the API's SOFT delete (enabled=false stamps retired_at; ISO 27001, the row is
// never hard-deleted).
const _AI_KB_ROWS = ['mod_docs', 'tenant_files', 'uploads'];
const _AI_KB_LABELS = {
    mod_docs: 'MOD Docs',
    tenant_files: 'Tenant files',
    uploads: 'Uploads',
};
let _aiKbExternals = [];
let _aiKbChosen = {};
let _aiKbScope = {};

// The option list for one knowledge-source row. Stacks come from §7.1's read
// (`_aiStacksLast`), filtered to the API's own kind read `embedding` — exactly
// how the embedding rows of the slots table build their options. External
// endpoints carry no capability in the API, so they are offered as the operator
// configured them and labelled as such.
function _aiKbFillOptions(sel) {
    const stacks = (Array.isArray(_aiStacksLast?.stacks) ? _aiStacksLast.stacks : [])
        .filter((st) => st?.kind === 'embedding');
    const ph = document.createElement('option');
    ph.value = '';
    ph.textContent = 'Choose…';
    sel.appendChild(ph);
    if (_paidEditionKnown) {
        const auto = document.createElement('option');
        auto.value = 'auto';
        auto.textContent = 'Let MOD choose';
        sel.appendChild(auto);
    }
    stacks.forEach((st) => {
        const opt = document.createElement('option');
        opt.value = `stack:${st.stack_id}`;
        opt.textContent = `${st.name || ''} — ${_aiStackStatusWord(st.status)}` +
            ((st.served_models || []).length > 1 ? ` (${(st.served_models || []).length} models)` : '');
        sel.appendChild(opt);
    });
    _aiKbExternals.forEach((ex, i) => {
        const opt = document.createElement('option');
        opt.value = `ext:${i}`;
        opt.textContent = `${ex.url}${ex.models.length ? ` — ${ex.models.join(', ')}` : ''}` +
            ' (external endpoint — the API does not report whether it embeds)';
        sel.appendChild(opt);
    });
    return stacks.length;
}

// The label for a knowledge-source pick this page made.
function _aiKbValueLabel(value) {
    if (!value || value === 'auto') return 'Let MOD choose';
    if (value.startsWith('stack:')) {
        const st = (Array.isArray(_aiStacksLast?.stacks) ? _aiStacksLast.stacks : [])
            .find((s) => String(s?.stack_id) === value.slice(6));
        return st ? `${st.name || ''} — ${_aiStackStatusWord(st.status)}` : `stack #${value.slice(6)}`;
    }
    if (value.startsWith('ext:')) {
        const ex = _aiKbExternals[Number(value.slice(4))];
        return ex ? ex.url : 'external endpoint';
    }
    return value;
}

// One row's one-line state sentence — the three facts the API keeps distinct:
// nobody ever bound it (`unbound` → "Not set"), a row exists and answers
// ("served by …"), and a row exists but is retired ("Off"). A tenant override
// is named as such so the operator can see WHICH scope answers for them.
function _aiKbStateText(row) {
    const eff = row?.effective_binding;
    if (row?.unbound || !eff) return 'Not set — no embedding model is bound for this source';
    const who = eff.scope === 'tenant' ? "this tenant's override" : 'the platform default';
    const what = eff.target_kind === 'external' ? (eff.base_url || 'external endpoint')
        : eff.target_kind === 'auto' ? 'MOD chooses'
        : (eff.stack_name || eff.served_model || `stack #${eff.stack_id ?? ''}`);
    const served = `${what}${eff.served_model && eff.target_kind === 'stack' ? ` — ${eff.served_model}` : ''}`;
    return eff.enabled
        ? `served by ${served} (${who})`
        : `Off — that binding was retired (soft delete, never erased)`;
}

function _renderAiKnowledgeSources(data) {
    const box = document.getElementById('ai-knowledge-sources');
    if (!box) return;
    const rows = Array.isArray(data?.rows) ? data.rows : [];
    const byKind = {};
    rows.forEach((r) => { if (r?.source_kind) byKind[r.source_kind] = r; });
    _aiKbExternals = _aiSlotCollectExternals();

    const head = document.createElement('h3');
    head.className = 'text-lg font-medium text-gray-800';
    head.textContent = 'Knowledge sources';
    box.innerHTML = '';
    box.appendChild(head);
    const intro = document.createElement('p');
    intro.className = 'text-xs text-gray-500';
    intro.textContent =
        'One row per assistant knowledge source. Each picks its OWN embedding model — a stack that serves ' +
        'chat models cannot be picked here. "Turn off" retires the binding (it is never erased).';
    box.appendChild(intro);

    const renderOne = (kind) => {
        const row = byKind[kind];
        const eff = row?.effective_binding;
        const live = row && !row.unbound && eff;
        const el = document.createElement('div');
        el.className = 'rounded-lg border border-gray-200 bg-white p-3 space-y-1 mb-3';
        el.dataset.kbKind = kind;

        const top = document.createElement('div');
        top.className = 'flex items-center gap-2 text-sm';
        const dot = document.createElement('span');
        dot.className = `inline-block w-2 h-2 rounded-full ${row?.unbound ? 'bg-gray-300' : (eff?.enabled ? 'bg-green-500' : 'bg-red-500')}`;
        dot.title = row?.unbound ? 'nobody bound this source'
            : (eff?.enabled ? 'embedding model answers' : 'binding retired — source is off');
        const name = document.createElement('span');
        name.className = 'font-medium text-gray-800';
        name.textContent = _AI_KB_LABELS[kind] || kind;
        top.appendChild(dot);
        top.appendChild(name);
        el.appendChild(top);

        const sel = document.createElement('select');
        sel.dataset.field = 'kb_binding';
        sel.dataset.kbKind = kind;
        sel.className = 'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm';
        _aiKbFillOptions(sel);
        const chosen = _aiKbChosen[kind];
        if (chosen && [...sel.options].some((o) => o.value === chosen)) sel.value = chosen;
        el.appendChild(sel);

        // "Turn off" — only offered where a LIVE row exists to retire; the API
        // refuses to retire a position it has no row for (422), and this page
        // says so instead of offering a dead button.
        if (live) {
            const off = document.createElement('button');
            off.className = 'px-2 py-1 rounded-md text-xs text-gray-700 border border-gray-300';
            off.dataset.kbAct = 'turn-off';
            off.dataset.kbKind = kind;
            off.textContent = 'Turn off';
            off.addEventListener('click', () => { _aiKbTurnOff(kind); });
            el.appendChild(off);
        }

        // A tenant that spoke (retired its override) does NOT inherit the
        // platform default — the API says so with `tenant_binding` +
        // `effective_scope`; name it so the operator knows why they see a
        // platform row for a source their tenant turned off.
        if (row?.tenant_binding && !row.tenant_binding.enabled && eff && eff.scope !== 'tenant') {
            const note = document.createElement('div');
            note.className = 'text-xs text-gray-500 mt-2';
            note.textContent = 'this tenant turned this source off — its override is retired, not inherited';
            el.appendChild(note);
        }

        // A pick this page made but the API has no row for yet — label the
        // dropdown's own value so the operator can see "you chose X, nothing
        // is bound yet" rather than a bare "Not set".
        if (row?.unbound && _aiKbChosen[kind]) {
            const picked = document.createElement('div');
            picked.className = 'text-xs text-gray-500 mt-2';
            picked.textContent = `you picked ${_aiKbValueLabel(_aiKbChosen[kind])} — save it to bind`;
            el.appendChild(picked);
        }

        const foot = document.createElement('div');
        foot.className = 'text-xs text-gray-500';
        foot.dataset.role = 'kb-status';
        foot.textContent = _aiKbStateText(row);
        el.appendChild(foot);
        box.appendChild(el);
    };

    _AI_KB_ROWS.forEach(renderOne);
    box.classList.remove('hidden');
}

// One-line notice for a failed knowledge-sources read — same shape as the
// slots table's: the section either shows its rows or says why it cannot.
function _aiKbNotice(detail) {
    const box = document.getElementById('ai-knowledge-sources');
    if (!box) return;
    const line = document.createElement('p');
    line.className = 'text-xs text-gray-500';
    line.textContent = `Knowledge sources unavailable: ${detail}`;
    const prev = box.querySelector('[data-ai-kb-notice]');
    if (prev) { prev.textContent = line.textContent; return; }
    line.setAttribute('data-ai-kb-notice', '1');
    box.appendChild(line);
    if (!box.querySelector('[data-kb-kind]')) box.classList.remove('hidden');
}

async function _loadAiKnowledgeSources() {
    // Unlike the who-uses-what read, this one is NOT platform-admin-only: the
    // API gates it like /platform/ai-config/tenant-bindings (an active,
    // onboarded user), because a tenant's own override is exactly what a
    // tenant user is allowed to see. The WRITE scope is decided per row.
    const token = _authToken();
    if (!token) return;
    let data = null, detail = '';
    try {
        const r = await fetch(`${window.APP_CONFIG?.API_URL || ''}/platform/ai-config/knowledge-sources`,
            { headers: { Authorization: `Bearer ${token}` } });
        if (r.ok) data = await r.json();
        else detail = `HTTP ${r.status}`;
    } catch (e) {
        detail = (e && e.message) || String(e);
    }
    if (!data) {
        _aiKbNotice(detail || 'the knowledge-sources endpoint did not answer');
        return;
    }
    _renderAiKnowledgeSources(data);
}

// The write path a row takes: platform admin → the platform default; a plain
// tenant user → its own override. Never both, and never a scope the API would
// 403/422 for.
function _aiKbWritePath(kind) {
    const useTenant = !(currentUser && currentUser.is_platform_admin);
    return useTenant
        ? `/platform/ai-config/knowledge-sources-tenant/${encodeURIComponent(kind)}`
        : `/platform/ai-config/knowledge-sources/${encodeURIComponent(kind)}`;
}

// Saving a row: PUT the scope's row with the pick the operator made. A 422
// (e.g. a chat stack the API refuses, "serves chat models, not embedding")
// is shown INLINE on that row in the API's own words, not as a generic
// "save failed" and not as a swallowed error.
async function _aiKbSave(kind, value) {
    const body = { target_kind: 'auto', enabled: true };
    if (value && value !== 'auto') {
        if (value.startsWith('stack:')) {
            body.target_kind = 'stack';
            body.stack_id = Number(value.slice(6));
        } else if (value.startsWith('ext:')) {
            const ex = _aiKbExternals[Number(value.slice(4))];
            if (!ex) return false;
            body.target_kind = 'external';
            body.base_url = ex.url;
            if (ex.models.length === 1) body.model = ex.models[0];
        }
    }
    const el = document.getElementById('ai-knowledge-sources')?.querySelector(`[data-kb-kind="${kind}"]`);
    try {
        const r = await _masterFetch(_aiKbWritePath(kind), { method: 'PUT', body: JSON.stringify(body) });
        _aiKbChosen[kind] = value;
        if (r?.source_kind) _aiKbScope[kind] = r.scope;
        if (el) _aiKbApplyResult(el, r);
        return r || true;
    } catch (e) {
        const msg = (e && e.message) || String(e);
        if (el) {
            const foot = el.querySelector('[data-role="kb-status"]');
            if (foot) foot.innerHTML = `<span class="text-red-600">${_esc(msg)}</span>`;
        } else _aiKbNotice(msg);
        return false;
    }
}

// Redraw ONE row from the result its own PUT returned — never a full re-render,
// which would reset every other row's dropdown under the operator.
function _aiKbApplyResult(el, r) {
    const dot = el.querySelector('span');
    if (dot) dot.className = `inline-block w-2 h-2 rounded-full ${r?.enabled ? 'bg-green-500' : 'bg-red-500'}`;
    const foot = el.querySelector('[data-role="kb-status"]');
    if (!foot) return;
    if (!r) { foot.textContent = 'Not set — no embedding model is bound for this source'; return; }
    const what = r.target_kind === 'external' ? (r.base_url || 'external endpoint')
        : r.target_kind === 'auto' ? 'MOD chooses'
        : (r.stack_name || r.served_model || `stack #${r.stack_id ?? ''}`);
    foot.textContent = r.enabled
        ? `served by ${what}${r.served_model && r.target_kind === 'stack' ? ` — ${r.served_model}` : ''} (${r.scope === 'tenant' ? "this tenant's override" : 'the platform default'})`
        : `Off — that binding was retired (soft delete, never erased)`;
    if (r.message) {
        const m = document.createElement('div');
        m.className = 'text-xs text-gray-500 mt-2';
        m.textContent = r.message;
        el.appendChild(m);
    }
}

// "Turn off" = the API's SOFT delete: PUT enabled=false, which stamps retired_at
// and never hard-deletes (ISO 27001). Only a row this page rendered as LIVE is
// retired — the API refuses to retire a source it has no live row for, so a
// "Not set" row gets no button at all.
async function _aiKbTurnOff(kind) {
    const box = document.getElementById('ai-knowledge-sources');
    const el = box?.querySelector(`[data-kb-kind="${kind}"]`);
    if (!el) return;
    try {
        const r = await _masterFetch(_aiKbWritePath(kind),
            { method: 'PUT', body: JSON.stringify({ enabled: false }) });
        _aiKbApplyResult(el, r || { enabled: false, scope: _aiKbScope[kind] || 'platform', target_kind: 'auto' });
        const off = el.querySelector('button[data-kb-act="turn-off"]');
        if (off) off.parentNode.removeChild(off);
    } catch (e) {
        const msg = (e && e.message) || String(e);
        const foot = el.querySelector('[data-role="kb-status"]');
        if (foot) foot.innerHTML = `<span class="text-red-600">${_esc(msg)}</span>`;
        else _aiKbNotice(msg);
    }
}

// Wire the knowledge-source dropdowns once: a `change` remembers the pick and
// writes it; a `change` back to "" (the Choose… placeholder) is a no-op —
// turning a source off is the explicit "Turn off" action, never a dropdown
// left open.
function _wireAiKnowledgeSources() {
    const box = document.getElementById('ai-knowledge-sources');
    if (!box || box.dataset.aiKbWired) return;
    box.dataset.aiKbWired = '1';
    box.querySelectorAll('select[data-field="kb_binding"]').forEach((sel) => {
        sel.addEventListener('change', async (e) => {
            const kind = e.target.dataset.kbKind;
            if (!kind) return;
            const v = e.target.value;
            if (!v) return;
            await _aiKbSave(kind, v);
        });
    });
}

// The ten per-tool tabs stay, but their execution-mode dropdowns do not
// render: the ai-tool-template ships none (see the "no per-tool execution-
// mode dropdown" note), so this is the belt-and-braces pass for an older
// build that still has one in the DOM — hide it, never delete the tab.
function _hideLegacyExecutionModeSelects() {
    document.querySelectorAll('[data-field="execution_mode"]').forEach((sel) => {
        sel.classList.add('hidden');
    });
}


// Accelerator support for the selected rung, from
// GET /persistent-stacks/accelerator-kinds?preset=<rung>.
//
// Three separate facts, deliberately not collapsed into one badge:
//   verified          — has this kind ever actually been RUN here
//   present_in_fleet  — does this deployment own that hardware
//   compatibility     — does THIS rung have an honest quantization on it
//
// "Untested" and "you do not own this" are different answers to "why is this
// greyed out", and only one of them is fixable by buying a card. Every kind
// stays selectable: the product decision is warn-and-allow, not block.
let _acceleratorKindsCache = {};

async function _renderAcceleratorSupport(form, preset) {
    const box = form.querySelector('[data-field-help="accelerator_support"]');
    if (!box) return;
    const token = _authToken();
    let data = _acceleratorKindsCache[preset];
    if (!data) {
        try {
            const r = await fetch(
                `${window.APP_CONFIG?.API_URL || ''}/stacks/accelerator-kinds?preset=${encodeURIComponent(preset)}`,
                { headers: { Authorization: `Bearer ${token}` } });
            if (!r.ok) return;
            data = await r.json();
            _acceleratorKindsCache[preset] = data;
        } catch { return; }
    }
    // The panel may have been re-rendered while that was in flight.
    const live = form.querySelector('[data-field-help="accelerator_support"]');
    if (!live) return;

    // A CPU tier declares no accelerator, so listing four of them and marking
    // each one "supported" would be noise. Say the one true thing instead.
    const tier = _tier(preset);
    if (tier && tier.compute_target === 'cpu') {
        live.innerHTML =
            '<div class="font-medium">Accelerator support for this tier</div>' +
            '<div class="mt-0.5 text-success/80">This tier runs on CPU and uses no accelerator, ' +
            'so it will place on any worker with enough RAM.</div>';
        return;
    }

    const rows = (data.kinds || []).map((k) => {
        const compat = k.compatibility || {};
        // An unsupported combination is reported as unsupported, naming what
        // DOES work there. It is never silently downgraded to a quantization
        // that will not fit — Marlin is CUDA-only, so an AWQ rung has no
        // honest AMD answer and pretending otherwise produces a stack that
        // fails at load time with no explanation.
        const supported = compat.supported !== false;
        const mark = supported
            ? '<span class="text-success">&check;</span>'
            : '<span class="text-destructive font-semibold">&times;</span>';

        const tags = [];
        if (!k.verified) tags.push('<span class="text-warning">unverified</span>');
        if (!k.present_in_fleet) tags.push('<span class="text-gray-600">not in this fleet</span>');
        const tagTxt = tags.length ? ` <span class="text-xs">(${tags.join(', ')})</span>` : '';

        // `warnings` already names what DOES work there ("Available there:
        // [...]"), which is the honest answer 6.7 asks for — never a silent
        // downgrade to a quantization that will not fit.
        const notes = [];
        (compat.warnings || []).forEach((w) => notes.push(w));
        if (!notes.length && !k.verified && k.note) notes.push(k.note);
        if (supported && compat.quantization) {
            notes.push(`Quantization used here: ${compat.quantization}.`);
        }
        if (!k.present_in_fleet) {
            notes.push('No worker in this deployment reports this accelerator, so a stack targeting it has nowhere to land.');
        }
        const why = notes.map((n) => `<div class="pl-4 text-success/80">${_esc(n)}</div>`).join('');
        return `<div>${mark} ${_esc(k.label)}${tagTxt}</div>${why}`;
    }).join('');

    live.innerHTML =
        '<div class="font-medium">Accelerator support for this tier</div>' +
        `<div class="mt-0.5 space-y-0.5">${rows}</div>` +
        '<div class="mt-1 text-success/80">Only CUDA has been run on real hardware here. ' +
        'The others are offered because the configuration is the same, not because they have been proven.</div>';
}


// Populate the tier <select> from the served ladder, cheapest rung first.
// §5.1 — only roles the ACTIVE tier actually serves.
//
// An option that cannot resolve is worse than an absent one: it reads as a
// working choice and fails at call time. `compact` served no reasoner until the
// pair landed, and on `minimal` there still is none.
function _populateRoleSelect(form, presetName) {
    const sel = form.querySelector('[data-field="local_role"]');
    if (!sel) return;
    const t = _tier(presetName || _tierLadder.default);
    const roles = (t && t.placement && t.placement.roles) || [];
    const cur = sel.value;
    sel.innerHTML = '';
    roles.forEach((r) => {
        const opt = document.createElement('option');
        // VALUE stays the internal role (the backend keys on it); the label is
        // the served model and the memory it needs — the same "model — N GB"
        // shape the catalog picker uses — never the role word.
        opt.value = r.role;
        const mem = (r.vram_floor_gb != null) ? ` — ${r.vram_floor_gb} GB` : '';
        opt.textContent = `${r.model || '?'}${mem}`;
        sel.appendChild(opt);
    });
    if (!roles.length) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'This tier serves no models';
        sel.appendChild(opt);
    }
    if (cur && roles.some((r) => r.role === cur)) sel.value = cur;

    const help = form.querySelector('[data-field-help="local_role_summary"]');
    if (help) {
        const chosen = roles.find((r) => r.role === sel.value);
        help.innerHTML = chosen
            ? `Currently served by <span class="font-medium">${_esc(chosen.model || '?')}</span>` +
              ` &middot; ${_esc(String(chosen.vram_floor_gb))} GB &middot; ${_esc(chosen.serving_stack || '')}` +
              `<div class="text-success/80">Change tier and this follows automatically.</div>`
            : '';
    }
}

// Node-Wizard-only: the "Code review" dropdown. VALUES stay self|reasoner|off
// (the backend maps them to roles), but the LABELS name the LLM each maps to on
// the active tier, so a change of tier renames them automatically — the same
// pattern as the role select above. Absent on non-wizard forms (removed there).
function _populateCriticSelect(form, presetName) {
    const sel = form.querySelector('[data-field="critic_mode"]');
    if (!sel) return;
    const t = _tier(presetName || _tierLadder.default);
    const roles = (t && t.placement && t.placement.roles) || [];
    const builder = roles.find((r) => r.role === 'generator') || {};
    const builderModel = builder.model;
    const builderMem = (builder.vram_floor_gb != null) ? ` (${builder.vram_floor_gb} GB)` : '';
    const cur = sel.value;
    sel.innerHTML = '';
    const mk = (val, text) => {
        const o = document.createElement('option');
        o.value = val; o.textContent = text; sel.appendChild(o);
    };
    // LABELS name the SERVED MODELS, never the role word: "self" means the
    // builder model, the separate option names the review model the tier
    // serves (or just "your own" when it serves none).
    const reviewer = roles.find((r) => r.role === 'reasoner');
    mk('self', `Self-review${builderModel ? ' — ' + builderModel + builderMem : ''} (recommended)`);
    mk('reasoner', reviewer && reviewer.model
        ? `Separate review model — ${reviewer.model}`
        : 'Separate review model (bring your own)');
    mk('off', 'Off — built-in checks only');
    sel.value = (cur && Array.from(sel.options).some((o) => o.value === cur)) ? cur : 'self';
    _populateCriticTierNote(form, presetName);
    _applyCriticMode(form);
}

// separate-reasoner-model — the reviewer/reasoner config panel below the
// critic_mode dropdown. Shown only when "Separate reasoner model" is chosen.
// The reviewer is the `critic` pseudo-tool: leave the fields blank and the
// review runs on the active tier's reasoner (managed); set a model and it runs
// on that independent model. Fields use data-critic-field to avoid colliding
// with the primary "Managed model" block's data-field.
function _applyCriticMode(form) {
    if (!form) return;
    const sel = form.querySelector('[data-field="critic_mode"]');
    const panel = form.querySelector('[data-critic-panel]');
    if (!sel || !panel) return;
    panel.classList.toggle('hidden', sel.value !== 'reasoner');
}

function _populateCriticTierNote(form, presetName) {
    const note = form && form.querySelector('[data-role="critic-tier-note"]');
    if (!note) return;
    const t = _tier(presetName || _tierLadder.default);
    const roles = (t && t.placement && t.placement.roles) || [];
    const reviewer = roles.find((r) => r.role === 'reasoner');
    if (reviewer && reviewer.model) {
        const rMem = (reviewer.vram_floor_gb != null) ? ` (${reviewer.vram_floor_gb} GB)` : '';
        note.innerHTML = `Leave the fields blank to review with the active tier's managed model ` +
            `&mdash; currently <strong>${_esc(reviewer.model)}</strong>${_esc(rMem)}. ` +
            `Set a Provider / Model / Base URL to review with your own model instead.`;
    } else {
        note.innerHTML = `This tier serves no separate review model. Set a Provider / Model / ` +
            `Base URL below to review with your own model (e.g. a cloud endpoint), ` +
            `or leave blank and the review falls back to the builder model.`;
    }
}

function _captureCriticState(form) {
    if (!form) return;
    const st = _aiFormState.critic || (_aiFormState.critic = {});
    form.querySelectorAll('[data-critic-field]').forEach((el) => {
        const key = el.dataset.criticField;
        if (!key) return;
        st[key] = el.value;
    });
}

function _hydrateCriticPanel(form, config) {
    if (!form) return;
    const cfg = config || {};
    form.querySelectorAll('[data-critic-field]').forEach((el) => {
        const key = el.dataset.criticField;
        if (!key) return;
        const val = cfg[key];
        if (el.type === 'password') {
            // api_key comes back as a bool from the server — placeholder only.
            if (typeof val === 'boolean') {
                el.value = '';
                el.placeholder = val ? 'Configured (enter new value to change)' : 'Leave blank to keep existing';
            } else if (typeof val === 'string') {
                el.value = val;
            }
            return;
        }
        if (val === undefined || val === null) return;
        el.value = String(val);
    });
}

// Build the PUT body for the separate reviewer/reasoner (critic pseudo-tool).
// Only persisted when "Separate reasoner model" is chosen. With no model/URL
// set it saves execution_mode 'local' so the review falls back to the tier's
// reasoner role; with a model it saves 'advanced' with that independent model.
function _buildCriticBody() {
    const st = _aiFormState.critic || {};
    // GE.AIC.1-3 — a chosen stack is also "a binding set", same as a typed
    // model/base_url; without this a stack pick here would silently save
    // execution_mode 'local' and drop the stack.
    const hasModel = (st.model && String(st.model).trim()) ||
                     (st.base_url && String(st.base_url).trim()) ||
                     (st.provider === 'stack' && st.stack_id);
    if (!hasModel) return { execution_mode: 'local' };
    const body = { execution_mode: 'advanced' };
    const criticFields = ['provider', 'model', 'base_url', 'compute_target', 'keep_alive', 'temperature', 'max_tokens'];
    if (st.provider === 'stack') criticFields.push('stack_id', 'served_model');
    criticFields.forEach((k) => {
        if (st[k] !== undefined && st[k] !== '') body[k] = st[k];
    });
    if (st.api_key && typeof st.api_key === 'string' && st.api_key !== '') {
        body.api_key = st.api_key;
    }
    return body;
}

function _populatePresetSelect(form) {
    const sel = form.querySelector('[data-field="local_preset"]');
    if (!sel || !(_tierLadder.ladder || []).length) return;
    const current = sel.value;
    sel.innerHTML = '';
    _tierLadder.ladder.forEach((t) => {
        const opt = document.createElement('option');
        opt.value = t.preset;
        // Unavailable tools are named in the option itself, so the cost of a
        // rung is legible in the closed dropdown and not only after selecting.
        const off = (t.unavailable_tools || []).length
            ? `  \u2014 no ${t.unavailable_tools.map((x) => _TOOL_LABELS[x] || x).join(', ')}`
            : '';
        // per-rung-reasoner \u00a75.3 \u2014 a rung this hardware cannot host is marked in
        // the CLOSED dropdown, not discovered after selecting it. Learning it
        // afterwards costs a stack that provisions, waits and never runs, with
        // the reason buried in placement arithmetic nobody reads.
        //
        // Still selectable: an operator may be choosing ahead of hardware
        // arriving, and the reason is stated in full in the summary below.
        const pl = t.placement;
        const unfit = pl && pl.placeable === false
            ? `  \u2014 WON'T FIT (${_esc(t.hardware_requirement || 'more hardware')})`
            : '';
        opt.textContent = `${t.label}${unfit}${off}`;
        sel.appendChild(opt);
    });
    if (current && _tier(current)) sel.value = current;
    _populateRoleSelect(form, sel.value);
    _populateCriticSelect(form, sel.value);
}

function _hydrateForm(tool, config) {
    const form = document.querySelector(`#ai-tool-${tool} form`);
    if (!form) return;
    // execution_mode is set by caller; other fields pulled from stashed state.
    form.querySelectorAll('[data-field]').forEach((el) => {
        const key = el.dataset.field;
        if (!key) return;
        // Never yank a control out from under the operator. If it currently has
        // focus (a dropdown mid-open, an input mid-type) leave it exactly as it
        // is — overwriting .value here is what reset a selection the user had
        // just made. The in-memory state is kept current by the form's own
        // change handler (_captureFormState), so nothing is lost by skipping it.
        if (el === document.activeElement) return;
        const val = config[key];
        if (val === undefined || val === null) return;
        if (el.type === 'password') {
            // API key is returned as a bool from the server — show placeholder only.
            if (typeof val === 'boolean') {
                el.value = '';
                el.placeholder = val ? 'Configured (enter new value to change)' : 'Paste key...';
            } else if (typeof val === 'string') {
                el.value = val;
            }
        } else if (el.tagName === 'SELECT' || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
            el.value = typeof val === 'boolean' ? (val ? 'true' : '') : String(val);
        }
    });
}

// GE.MNL.4b — per-tool LLM fallback. The checkbox/select use `data-role`, not
// `data-field`, because a plain checkbox does not fit the generic data-field
// capture/hydrate above (a checkbox's `.value` is not its checked state, and
// `_captureFormState`/`_hydrateForm` only ever read/write `.value`).

// "Served model name where available" — prefer what the tier actually
// resolved the tool's role to (managed mode), then whatever model/local_model
// was saved (advanced/cloud/self-hosted). Never invents a name.
function _fallbackModelLabel(cfg) {
    if (!cfg) return '';
    return cfg.resolved_role_model || cfg.model || cfg.local_model || '';
}

// Every OTHER configurable tool — never the one whose panel this is, matching
// the backend's self-reference 400 in update_tool_ai_config(). `allConfigs` is
// the GET /platform/ai-config/all response, used only to label options with
// what that tool is currently pointed at.
function _populateFallbackSelect(tool, form, allConfigs) {
    const sel = form && form.querySelector('[data-role="fallback-tool"]');
    if (!sel) return;
    const state = _aiFormState[tool] || {};
    const want = state.fallback_tool || '';
    sel.innerHTML = '';
    const blank = document.createElement('option');
    blank.value = '';
    blank.textContent = 'Choose a tool…';
    sel.appendChild(blank);
    AI_TOOLS.filter((t) => t !== tool).forEach((t) => {
        const label = _TOOL_LABELS[t] || t;
        const modelName = _fallbackModelLabel((allConfigs && allConfigs[t]) || {});
        const opt = document.createElement('option');
        opt.value = t;
        opt.textContent = modelName ? `${label} — ${modelName}` : label;
        sel.appendChild(opt);
    });
    sel.value = Array.from(sel.options).some((o) => o.value === want) ? want : '';
}

// Select is only meaningful while the checkbox is on — disabled otherwise so
// a stale target can't be saved silently.
function _applyFallbackToggle(form) {
    const cb = form && form.querySelector('[data-role="fallback-enabled"]');
    const sel = form && form.querySelector('[data-role="fallback-tool"]');
    if (!cb || !sel) return;
    sel.disabled = !cb.checked;
}

function _hydrateFallbackPanel(tool, form, state) {
    if (!form) return;
    const cb = form.querySelector('[data-role="fallback-enabled"]');
    const sel = form.querySelector('[data-role="fallback-tool"]');
    const cfg = state || {};
    if (cb && cb !== document.activeElement) cb.checked = !!cfg.fallback_enabled;
    if (sel && sel !== document.activeElement && cfg.fallback_tool !== undefined) {
        const has = Array.from(sel.options).some((o) => o.value === cfg.fallback_tool);
        sel.value = has ? cfg.fallback_tool : '';
    }
    _applyFallbackToggle(form);
}

function _captureFallbackState(tool, form) {
    if (!form) return;
    const state = _aiFormState[tool] || (_aiFormState[tool] = {});
    const cb = form.querySelector('[data-role="fallback-enabled"]');
    const sel = form.querySelector('[data-role="fallback-tool"]');
    if (cb) state.fallback_enabled = !!cb.checked;
    if (sel) state.fallback_tool = sel.value || '';
}

function _switchTab(tool) {
    // GE.AIC.5/6 (2026-09-29): 'gpu' and 'models' no longer have a matching
    // .ai-tool-tab/.ai-tool-panel (both tabs were removed). A stale bookmark,
    // external link, or leftover call site asking for either — or for any
    // other tool name that no longer exists — must not leave every panel
    // hidden; fall back to the first visible tab instead.
    let target = tool;
    if (!document.querySelector(`.ai-tool-tab[data-tool="${target}"]`)) {
        const first = document.querySelector('.ai-tool-tab:not(.hidden)')
            || document.querySelector('.ai-tool-tab');
        target = first ? first.dataset.tool : target;
    }
    document.querySelectorAll('.ai-tool-tab').forEach((btn) => {
        const active = btn.dataset.tool === target;
        btn.classList.toggle('border-primary', active);
        btn.classList.toggle('text-primary', active);
        btn.classList.toggle('border-transparent', !active);
        btn.classList.toggle('text-gray-500', !active);
    });
    document.querySelectorAll('.ai-tool-panel').forEach((p) => {
        p.classList.toggle('hidden', p.dataset.tool !== target);
    });
}

// Why local mode is unavailable — for the path this deployment ACTUALLY uses.
//
// This banner used to describe one path unconditionally: it probed the `vllm`
// compose service and, finding nothing, told the operator to run
// the managed compose command on "a host with a GPU". On a deployment
// running the STACK path that is wrong three times over. The compose service is
// deliberately absent (its profile is disabled), the advice would start a
// service the operator's own configuration turned off, and blaming a missing
// GPU is a guess — the card can be present, working, and simply too small for
// the selected tier, which is the common case and the one the operator can act
// on.
//
// So: report the state of the path in use, and say what to DO.
// Refresh timer for the weight-download bar. One timer, cleared and re-armed,
// so repeated loadAIConfig() calls cannot stack up overlapping pollers.
let _dlRefreshTimer = null;

// The tier that was in the dropdown when the current download started. Pressing
// Apply on THAT again is the destructive case — it restarts the pull from zero.
// Picking a different tier is the opposite: it is how someone abandons a slow
// download and goes back to one already cached, so it must stay available.
let _downloadingPreset = null;

// ai-config-stack-binding §7.3b — the tier-Apply lock is gone. There is no
// tier selection and no tier deploy left on this page (model / tier /
// placement moved to Persistent Stacks → "New AI stack"), so the one action
// that used to throw a weights download away cannot be taken from here at all.
// The download itself is still watched and drawn by _scheduleDownloadRefresh /
// _pollAIStatus; only the button-locking half of that machinery had to go.
// Kept as a no-op because the status poll calls it on every tick.
function _applyDownloadLock() {
    _downloadingPreset = null;
}

function _scheduleDownloadRefresh() {
    if (_dlRefreshTimer) { clearTimeout(_dlRefreshTimer); _dlRefreshTimer = null; }

    // Polling ONLY while a download is already visible was wrong, and it is the
    // exact window where the user is watching. Right after Apply the container
    // has not started pulling yet, so `downloads` is empty — no poll was armed,
    // and the bar then never appeared at all until someone reloaded by hand.
    // The refresh has to survive the gap between "Apply pressed" and "bytes on
    // disk", which is where the whole feature is judged.
    //
    // So: also poll whenever managed AI is not serving. That covers deploying,
    // starting, and about-to-download, and it terminates on its own — once the
    // model answers, `healthy` goes true and there is nothing left to watch.
    const downloading = (_ollamaHealth.downloads || []).length > 0;
    const settling = _ollamaHealth.as_stacks && !_ollamaHealth.healthy;
    if (!downloading && !settling) return;
    // Faster while WAITING for a download to appear than while watching one
    // move: the gap after Apply is the part that feels broken, and a
    // multi-gigabyte bar does not need a 5s refresh once it is on screen.
    const delay = downloading ? 15000 : 5000;
    _dlRefreshTimer = setTimeout(() => {
        // Only if the panel is still on screen — a timer firing against a
        // page the user has left is pure waste.
        if (!document.getElementById('main-panel-ai')) return;
        // Poll the STATUS only — never loadAIConfig(). A full reload here
        // rebuilt every form (per-tool _hydrateForm + the master bar's tier
        // dropdown) from server state on each tick, which reset dropdowns and
        // wiped inputs the operator was mid-edit. _pollAIStatus fetches the
        // live health/stacks and patches only the dynamic status regions,
        // leaving the form controls untouched.
        //
        // .finally, not .then: if the refresh throws — the API restarting
        // mid-switch is an ordinary way for that to happen — the chain must
        // still continue. Re-arming only on success is how a single failed
        // request froze the bar until someone reloaded by hand.
        _pollAIStatus()
            .catch(() => {})
            .finally(() => { _scheduleDownloadRefresh(); });
    }, delay);
}

// Status-only refresh for the periodic poll (download progress / tier switch /
// deploy settling). This is the segmented counterpart to loadAIConfig(): where
// loadAIConfig() rebuilds the whole panel — re-fetching config, re-deriving
// _aiFormState, re-hydrating every per-tool form, and re-running the master bar
// (which rebuilds the tier dropdown) — this touches ONLY the live status nodes:
//
//   • the download / "starting…" / unavailable banner (_applyOllamaGating)
//   • the Apply lock on the tier being downloaded (_applyDownloadLock)
//   • the managed-AI stack-path state backing that banner (_refreshManagedAiState)
//
// It does NOT call _hydrateForm, does NOT rebuild _aiFormState, and does NOT
// call _initMasterBar/_loadMasterBar/_renderBudgetOptions — so no dropdown,
// text input, or checkbox the operator is editing is ever re-rendered or reset
// by a poll tick.
async function _pollAIStatus() {
    if (!document.getElementById('main-panel-ai')) return;
    const token = sessionStorage.getItem('accessToken');
    if (!token) return;
    // 2400LR-cdu: llm-health is ai_config.py:2586 and ai_config.py ships in CE
    // (open-core/manifest.yaml:801 api_allow, AA-CE comment at :800; mounted at
    // CORE/API/main.py:2343), so the poll runs on CE too — leaving it gated froze
    // the download bar on CE.

    try {
        const healthResp = await fetch(
            `${window.APP_CONFIG?.API_URL || ''}/platform/llm-health`,
            { headers: { Authorization: `Bearer ${token}` } });
        if (healthResp.ok) {
            _ollamaHealth = await healthResp.json();
        }
        // A failed probe deliberately leaves _ollamaHealth untouched — the
        // last known state is a better thing to keep showing than an empty
        // panel, and the next tick corrects it. This mirrors loadAIConfig().
    } catch (_) { /* keep last-known state; the next tick corrects it */ }

    // Patch the status regions. None of these rebuild a form control:
    // _applyDownloadLock toggles the Apply button's disabled/title only;
    // _applyOllamaGating rewrites the banner's innerHTML and flips the
    // execution_mode <option>'s disabled/label (never the select's value);
    // _refreshManagedAiState re-fetches the stack-path state that banner reads.
    _applyDownloadLock();
    _applyOllamaGating();
    try { await _refreshManagedAiState(); } catch (_) { /* best-effort */ }
}

function _localUnavailableMessage(stillServing = false) {
    const ms = _managedAiState;

    // Compose path (or unknown): the original advice, which is correct there.
    if (!ms || !ms.enabled) {
        return `<strong>Local model not set yet:</strong> this Community Edition deployment brings ` +
            `its own models, so there is no managed service to start. Choose Cloud API, Self-hosted ` +
            `Ollama, or Advanced on one of the tool tabs (or connect a worker that serves a model), ` +
            `then reload this page.`;
    }

    // MEASURED progress wins over any inference below. The worker reports
    // bytes on disk each heartbeat and the manager joins the total from
    // HuggingFace, so this is the real figure rather than "probably
    // downloading" — which said the same words whether or not it was true.
    const dls = _ollamaHealth.downloads || [];
    if (dls.length) {
        const rows = dls.map((d) => {
            const mb = (d.downloaded_bytes / 1048576).toFixed(0);
            // Not started yet. Shown as a bar from the first moment rather than
            // withheld until bytes exist: the ~30s between Apply and the first
            // byte is when someone decides whether anything happened, and an
            // empty panel there reads as a failure. Striped and labelled so it
            // is honestly "starting", not a progress claim of 0%.
            if (d.state === 'pending') {
                const totalMb = d.total_bytes
                    ? ` — ${(d.total_bytes / 1048576).toFixed(0)} MB to fetch` : '';
                return `<div class="mt-2">`
                    + `<div class="flex justify-between text-xs mb-1">`
                    + `<span>${_esc(d.model)}</span>`
                    + `<span><strong>starting…</strong>${totalMb}</span></div>`
                    + `<div class="w-full bg-warning/20 rounded-full h-2 overflow-hidden">`
                    + `<div class="bg-warning h-2 rounded-full animate-pulse" style="width:100%"></div>`
                    + `</div></div>`;
            }
            if (d.percent === null || d.percent === undefined || !d.total_bytes) {
                // No denominator (no egress to look one up). Show what is
                // known and say what is not, rather than inventing a total —
                // a bar that moves smoothly and means nothing is worse than
                // no bar at all.
                return `<div class="mt-2"><div class="text-xs">${_esc(d.model)} — `
                    + `<strong>${mb} MB</strong> downloaded (total size unknown)</div></div>`;
            }
            const totalMb = (d.total_bytes / 1048576).toFixed(0);
            const pct = Math.max(0, Math.min(100, d.percent));
            return `<div class="mt-2">`
                + `<div class="flex justify-between text-xs mb-1">`
                + `<span>${_esc(d.model)}</span>`
                + `<span><strong>${pct}%</strong> — ${mb} / ${totalMb} MB</span></div>`
                + `<div class="w-full bg-warning/20 rounded-full h-2 overflow-hidden">`
                + `<div class="bg-warning h-2 rounded-full" style="width:${pct}%"></div>`
                + `</div></div>`;
        }).join('');
        const anyMoving = dls.some((d) => d.state !== 'pending');
        // The old model keeps answering until the new container replaces it, so
        // during the first stretch of a tier change managed AI is BOTH healthy
        // and downloading. Saying "not answering" there would be false.
        return `<strong>${anyMoving ? 'Downloading model weights.'
                                     : 'Preparing to download model weights.'}</strong> `
            + (stillServing
                ? `Your current model keeps answering until the new one is ready. `
                : `Managed AI is not answering yet because it is still fetching `
                  + `what this tier serves. `)
            + `This is a one-time download per tier — the weights are cached, so `
            + `coming back to a tier you have already run starts in seconds.`
            + rows
            + `<div class="mt-3 text-xs">Apply is locked for this tier while it `
            + `downloads — re-applying would restart it from zero. To change your `
            + `mind, pick a different tier and Apply unlocks; one you have already `
            + `downloaded starts in seconds. This panel updates on its own.</div>`;
    }

    // Stack path. The compose service is absent BY DESIGN here, so never
    // mention it — the question is what the stacks are doing.
    const stacks = ms.stacks || [];
    const unplaceable = stacks.filter((s) => s.placement && s.placement.placeable === false);
    const notProvisioned = stacks.filter((s) => !s.provisioned);
    const head = `<strong>Local mode unavailable:</strong> managed AI runs as <em>stacks</em> on this ` +
        `deployment, and none is currently serving. The <code>vllm</code> compose service is ` +
        `deliberately not used here, so starting it would not help.`;

    if (unplaceable.length) {
        // The actionable case: the hardware cannot host the selected tier.
        // Reuses the placement reason already computed server-side rather than
        // guessing at a cause.
        const lines = unplaceable.map((s) => {
            const p = s.placement || {};
            const fix = p.fits_instead
                ? ` The <strong>${_esc(p.fits_instead.preset)}</strong> tier ${_esc(p.fits_instead.reason)}.`
                : '';
            return `<li><code>${_esc(s.name)}</code>: ${_esc(p.reason || 'cannot be placed')}${fix}</li>`;
        }).join('');
        return head +
            `<div class="mt-2">Why:</div><ul class="list-disc ml-5 mt-1">${lines}</ul>` +
            `<div class="mt-2"><strong>What to do:</strong> pick a tier this hardware can host ` +
            `(see <em>Hardware tier</em> below and the <em>Managed AI stacks</em> panel for the ` +
            `shortfall), or add a card that meets the tier's requirement.</div>`;
    }

    if (notProvisioned.length) {
        return head +
            `<div class="mt-2"><strong>What to do:</strong> the managed-AI stacks have not been ` +
            `provisioned yet. Provision them from the <em>Managed AI stacks</em> panel below.</div>`;
    }

    // A stack that IS running but is not answering yet is the commonest case
    // by far, and the old text called it "not running" — which is false, and
    // sent the reader looking for a failure that had not happened. On a rung
    // this deployment has not served before, the server downloads several GB
    // of weights before it binds a port, and llama.cpp's fetch prints nothing
    // while it does. Saying so is the whole fix: the state is normal, it is
    // slow, and nobody can infer that from silence.

    const starting = stacks.filter(
        (s) => s.status === 'running' || s.status === 'deploying');
    if (starting.length) {
        const names = starting.map(
            (s) => `<code>${_esc(s.name)}</code> (${_esc(s.status)})`).join(', ');
        return `<strong>Managed AI is starting, not broken.</strong> ${names} ` +
            `${starting.length === 1 ? 'is' : 'are'} up but not answering yet.` +
            `<div class="mt-2">The most likely reason is the <strong>first-run model ` +
            `download</strong>. Changing to a tier this deployment has never served ` +
            `means fetching that tier's weights — commonly 1.5–25 GB, over your own ` +
            `connection, before the server accepts a single request. It reports no ` +
            `progress while it does, so it looks stalled when it is not.</div>` +
            `<div class="mt-2"><strong>What to do:</strong> wait, and re-check this page. ` +
            `On a slow link a 1.5 GB tier takes around 40 minutes. <strong>Do not press ` +
            `Apply again</strong> — redeploying restarts the download from zero. ` +
            `The weights are cached afterwards, so returning to a tier you have already ` +
            `run comes up in seconds.</div>` +
            `<div class="mt-2">If it is still like this well beyond that, check ` +
            `<em>System → Stacks</em>: a genuinely broken stack reads <code>failed</code>, ` +
            `not <code>running</code>.</div>`;
    }

    return head +
        `<div class="mt-2"><strong>What to do:</strong> the stacks exist but are not running. ` +
        `Check the <em>Managed AI stacks</em> panel below for each stack's status and reason.</div>`;
}

function _applyOllamaGating() {
    // Grey out the Local option when the managed LLM (vLLM) isn't reachable.
    // Also shows a "pending restart" banner when the saved preset differs
    // from what vLLM is currently serving.
    const healthy = _ollamaHealth.healthy;
    const mismatch = healthy && _ollamaHealth.running_matches_saved === false;

    AI_TOOLS.forEach((tool) => {
        const form = document.querySelector(`#ai-tool-${tool} form`);
        if (!form) return;
        const localOption = form.querySelector('[data-field="execution_mode"] option[value="local"]');
        if (localOption) {
            localOption.disabled = !healthy;
            // "vLLM service not running" names the compose service, which on a
            // stack deployment is not the thing that is missing.
            localOption.textContent = healthy
                ? 'Local (managed) — recommended'
                : (_managedAiState && _managedAiState.enabled
                    ? 'Local (managed) — no managed-AI stack is serving'
                    : 'Local (managed) — no local model is serving yet');
        }
    });

    const panel = document.getElementById('main-panel-ai');
    if (!panel) return;
    let banner = panel.querySelector('#ai-ollama-banner');

    // A download in flight is shown whether or not managed AI is healthy, and
    // this ordering is the whole reason the bar felt late. The panel used to
    // live behind `!healthy`, but immediately after Apply the OLD model is
    // still answering — healthy is true — so nothing rendered until the
    // container was torn down half a minute later. The bar was correct and
    // simply never drawn. Progress is not an unavailability message; it is its
    // own state, and it belongs first.
    const activeDownloads = (_ollamaHealth.downloads || []).length > 0;

    if (activeDownloads || !healthy) {
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'ai-ollama-banner';
            banner.className = 'bg-warning/10 border border-warning/40 rounded-lg p-4 text-sm text-gray-800 mb-4';
            panel.insertBefore(banner, panel.querySelector('.border-b'));
        }
        banner.innerHTML = _localUnavailableMessage(healthy);
    } else if (mismatch) {
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'ai-ollama-banner';
            banner.className = 'bg-warning/10 border border-warning/40 rounded-lg p-4 text-sm text-gray-800 mb-4';
            panel.insertBefore(banner, panel.querySelector('.border-b'));
        }
        // On the stack path the API redeploys the models itself, so the
        // compose remedy below is not merely unhelpful, it is wrong: there is
        // nothing for the operator to run. A mismatch here means the redeploy
        // is still in flight or it failed, and the place to look is the stack
        // list. Telling someone to run a command that cannot help is how they
        // conclude the feature does not work.
        // A switch in progress is not a fault. While a managed stack is
        // `deploying` this state is expected and lasts about half a minute, so
        // saying "that is a bug worth reporting" is alarming and wrong — it
        // describes the normal path. Only once nothing is deploying does a
        // lingering mismatch mean something actually needs looking at.
        const switching = ((_managedAiState && _managedAiState.stacks) || [])
            .some((s) => s.status === 'deploying');
        banner.innerHTML = _ollamaHealth.as_stacks
            ? (switching
                ? `<strong>Switching to ${_esc(_ollamaHealth.saved_preset)}.</strong> ` +
                  `Your current model keeps answering until the new one is ready — ` +
                  `usually well under a minute when the weights are already here. ` +
                  `This clears on its own.`
                : `<strong>Not serving your selection yet:</strong> you chose ` +
                  `<code>${_ollamaHealth.saved_preset}</code>, and the models still running are ` +
                  `<code>${(_ollamaHealth.served_models || []).join(', ') || 'none'}</code>. ` +
                  `Nothing is currently deploying, so this is not a switch in progress. ` +
                  `Check <strong>System → Stacks</strong> for <code>managed-ai-generator</code> ` +
                  `and <code>managed-ai-reasoner</code>: if either reads <code>failed</code>, ` +
                  `press Apply again, and if it stays failed that is worth reporting.`)
            : `<strong>Pending restart:</strong> saved preset <code>${_ollamaHealth.saved_preset}</code> differs from ` +
              `what vLLM is currently serving (<code>${(_ollamaHealth.served_models || []).join(', ')}</code>). ` +
              `Apply the new preset with: <code>MANAGED_OLLAMA_PRESET=${_ollamaHealth.saved_preset} docker compose up -d --force-recreate vllm</code>`;
    } else if (banner) {
        banner.remove();
    }
}

async function loadAIConfig() {
    const token = sessionStorage.getItem('accessToken');
    if (!token) return;

    // 1263 review nit (brief 1396) — clear the `data-hydrated` marker BEFORE any
    // fetch. The marker tells E2E/UI consumers the per-tool form shows fresh
    // settings; on a re-fetch (refresh, tab re-entry, save -> reload) it stayed
    // set from the previous load, so a consumer could read the stale render as
    // "hydrated". Clearing first means the marker is only ever true once the
    // NEW data has rendered below, and a fetch error (the early `return` on a
    // non-ok config response) leaves it unset instead of stale-true.
    document.querySelectorAll('.ai-tool-form').forEach((el) => {
        if (el.dataset.hydrated !== undefined) delete el.dataset.hydrated;
    });

    // Render sub-panels and wire tab switching once.
    AI_TOOLS.forEach(_renderToolSubPanel);
    document.querySelectorAll('.ai-tool-tab').forEach((btn) => {
        btn.addEventListener('click', () => _switchTab(btn.dataset.tool));
    });
    _wireMasterBar();
    // One Save persists all four Wizard cards (the per-card forms all route to
    // the same handler). Wire once — the button lives outside the templated forms.
    const _unifiedSave = document.getElementById('ai-unified-save');
    if (_unifiedSave && !_unifiedSave.dataset.wired) {
        _unifiedSave.dataset.wired = '1';
        _unifiedSave.addEventListener('click', (e) => handleSaveAllAIConfig(e));
    }

    // 2400LR-cdu: ai_config*.py + llm-health SHIP in CE (open-core/manifest.yaml:847
    // `CORE/API/routers/ai_config.py` under api_allow, AA-CE comment at :846; mounted at
    // CORE/API/main.py:2339), so the four reads below run on CE and the Advanced sections
    // paint. Only the managed-AI strip stays hidden: services/managed_ai_stacks.py is
    // denied at open-core/manifest.yaml:1565, so its read would 404.
    const _ceNoAiCfg = !(await isPaidEdition());
    if (_ceNoAiCfg) {
        for (const id of ['ai-stacks-section']) {
            document.getElementById(id)?.classList.add('hidden');
        }
    }
    const _aiFetch = (path) => fetch(`${window.APP_CONFIG?.API_URL || ''}${path}`, {
        headers: { Authorization: `Bearer ${token}` },
    });

    try {
        const [configResp, healthResp, catalogResp, tiersResp] = await Promise.all([
            _aiFetch('/platform/ai-config/all'),
            _aiFetch('/platform/llm-health'),
            _aiFetch('/platform/ai-config/model-catalog'),
            _aiFetch('/platform/ai-config/tiers'),
        ]);
        // GE.AIC.1-3 — stack options, needed before any slot is hydrated so a
        // slot pinned to "stack" can pre-select its stack/served-model.
        await _loadStackOptions();

        // ai-config-stack-binding §7.1 — the AI stacks section (its own fetch
        // pair; a failure leaves the last render + a one-line notice, never a
        // blank). Wired once for the row deep links.
        // CE: load the data the slot pickers read (_aiStacksLast) but keep the
        // section hidden (_aiStacksKeepHidden guards the un-hide lines). A failed
        // load never throws: pickers fall back to the no-stacks options.
        _aiStacksKeepHidden = _ceNoAiCfg;
        try {
            await _loadAiStacksSection();
        } catch (e) {
            if (!_ceNoAiCfg) throw e;
        }
        if (!_ceNoAiCfg) _wireAiStacksLinks();

        if (healthResp.ok) {
            _ollamaHealth = await healthResp.json();
        }
        // Re-armed OUTSIDE the ok-check, and it has to be. During a tier switch
        // the model server is deliberately down, which is exactly when the
        // health probe is most likely to fail — and arming the next poll only
        // on success meant one bad response stopped the refresh for good. The
        // page then froze on whatever it had last drawn ("starting…") while the
        // download ran to completion behind it, which is indistinguishable from
        // a stuck download and was reported as one.
        //
        // A failed probe also leaves _ollamaHealth untouched rather than
        // cleared: the last known state is a better thing to keep showing than
        // an empty panel, and the next poll corrects it.
        _scheduleDownloadRefresh();  // 2400LR-cdu: llm-health is CE-mounted (manifest:801), so the refresh runs on CE
        _applyDownloadLock();
        if (catalogResp.ok) {
            _modelCatalog = await catalogResp.json();
        }
        if (tiersResp.ok) {
            _tierLadder = await tiersResp.json();
        }
        _refreshManagedAiState();
        _applyOllamaGating();

        const resp = configResp;
        // BUG.2400LR-g6 — on !resp.ok (402/403/500) skip AI tools hydration
        // but let the sub-tab inits below run; the return here killed them.
        const all = resp.ok ? await resp.json() : {};

        // Uploaded embedding models — needed to populate the per-tool picker.
        await _loadEmbeddingModelList();

        AI_TOOLS.forEach((tool) => {
            const config = all[tool] || {};
            _aiFormState[tool] = {
                provider: config.provider || '',
                model: config.model || '',
                api_key: config.api_key,  // bool from server; mask placeholder only
                base_url: config.base_url || '',
                compute_target: config.compute_target || 'cpu',
                keep_alive: config.keep_alive || 'forever',
                temperature: config.temperature || '',
                max_tokens: config.max_tokens || '',
                local_preset: config.local_preset || 'balanced',
                local_model: config.local_model || _modelCatalog.default,
                // Read-only, served by the API: which role this tool takes in
                // managed mode, what that currently resolves to, and why it
                // does not when it cannot.
                local_role: config.local_role || null,
                resolved_role_model: config.resolved_role_model || null,
                role_unresolved_reason: config.role_unresolved_reason || null,
                // GE.AIC.1-3 — set only when provider === "stack".
                stack_id: config.stack_id || '',
                served_model: config.served_model || '',
                embedding_model: config.embedding_model || '',
                embedding_store: config.embedding_store || 'user_rag',
                embedding_model_user_rag: config.embedding_model_user_rag || '',
                embedding_model_docs: config.embedding_model_docs || '',
                // GE.AIC.1-3 — embedding provider ("" = platform default | "stack").
                embedding_provider: config.embedding_provider || '',
                embedding_stack_id: config.embedding_stack_id || '',
                embedding_provider_user_rag: config.embedding_provider_user_rag || '',
                embedding_stack_id_user_rag: config.embedding_stack_id_user_rag || '',
                embedding_provider_docs: config.embedding_provider_docs || '',
                embedding_stack_id_docs: config.embedding_stack_id_docs || '',
                num_ctx: config.num_ctx || null,
                // WIZARD only: how code review runs (self / reasoner / off).
                critic_mode: config.critic_mode || 'self',
                // Retained so _anyToolInAdvancedMode (the master-mode fallback
                // when nothing is stored locally) still reads a per-tool mode.
                execution_mode: config.execution_mode || 'local',
                // GE.MNL.4b — opt-in per-tool LLM fallback (mesh-native-llm-routing
                // §4.8). fallback_tool is always one of the OTHER configurable
                // tools, never this one — the backend 400s a self-reference.
                fallback_enabled: !!config.fallback_enabled,
                fallback_tool: config.fallback_tool || '',
            };
            const form = document.querySelector(`#ai-tool-${tool} form`);
            if (!form) return;

            // ai-configuration-page redesign — no per-tool execution-mode dropdown
            // and no per-tool hardware tier. The full "Managed model" block is the
            // config; fields are hydrated straight from the resolved config. The
            // hardware tier lives once in the master bar.

            // Per-tool trimmed embedding sections (embedding-relevant tools only).
            _injectEmbeddingSelect(tool, form, config);

            _hydrateForm(tool, _aiFormState[tool]);
            // `all` already holds every tool's config (fetched above), so option
            // labels can name what each OTHER tool currently resolves to.
            _populateFallbackSelect(tool, form, all);
            _hydrateFallbackPanel(tool, form, _aiFormState[tool]);
            form.dataset.hydrated = 'true';
            // GE.AIC.1-3 — reveal/hide the Stack picker vs. Base URL/API
            // Key/Compute Target/Keep Alive per this tool's own Provider value.
            _applyStackProviderToggle(form);
        });

        // separate-reasoner-model — load the separate reviewer/reasoner
        // (critic pseudo-tool) config and hydrate the Node Wizard's panel.
        // Independent of AI_TOOLS (it's not a top-level tab).
        const criticCfg = all.critic || {};
        _aiFormState.critic = {
            provider: criticCfg.provider || 'openai_compatible',
            model: (criticCfg.execution_mode && criticCfg.execution_mode !== 'local')
                ? (criticCfg.model || '') : '',
            api_key: criticCfg.api_key,  // bool from server; mask placeholder only
            base_url: (criticCfg.execution_mode && criticCfg.execution_mode !== 'local')
                ? (criticCfg.base_url || '') : '',
            compute_target: criticCfg.compute_target || 'cpu',
            keep_alive: criticCfg.keep_alive || 'forever',
            temperature: criticCfg.temperature || '',
            max_tokens: criticCfg.max_tokens || '',
            // GE.AIC.1-3 — set only when provider === "stack".
            stack_id: criticCfg.stack_id || '',
            served_model: criticCfg.served_model || '',
            execution_mode: criticCfg.execution_mode || 'local',
        };
        const _wizForm = document.querySelector('#ai-tool-wizard form');
        if (_wizForm) {
            _hydrateCriticPanel(_wizForm, _aiFormState.critic);
            const _wizPreset = (_aiFormState.wizard || {}).local_preset || _tierLadder.default;
            _populateCriticTierNote(_wizForm, _wizPreset);
            _applyCriticMode(_wizForm);
            // Re-run after the critic panel's own hydration so its Stack /
            // served-model pickers reflect the just-loaded critic stack pin.
            _applyStackProviderToggle(_wizForm);
        }
    } catch (err) {
        console.error('Failed to load AI config:', err);
    }

    // scheduler-runtime-config-panel — init the Scheduler sub-tab.
    // Idempotent (guarded by `_schedulerSubTabWired`).
    _initSchedulerSubTab();

    // δ §18 — init the Training sub-tab. Same admin-gated reveal as the
    // Scheduler tab; idempotent (guarded by `_trainingSubTabWired`).
    _initTrainingSubTab();

    // wizard-learning-from-feedback — init the Wizard Learning sub-tab.
    // Same admin gating; idempotent (guarded by `_wizardLearningWired`).
    _initWizardLearningSubTab();

    // KP.8 — init the Node Wizard Knowledge Pack sub-tab. Same admin
    // gating; idempotent (guarded by `_wizardKnowledgePackWired`).
    _initWizardKnowledgePackSubTab();

    // KEEPWARM-UI (owner Tim 11-25) — init the Keep-warm containers sub-tab.
    // Platform-admin only; idempotent (guarded by `_keepWarmWired`).
    _initKeepWarmSubTab();

    // tenant-resource-monitor §6 — init the Resource Monitor sub-tab.
    // Idempotent (guarded by `_resourceMonitorSubTabWired`).
    _initResourceMonitorSubTab();

    // mod-assistant-agent §9 — init the Agent sub-tab. Tenant-admin
    // (parent_user_id IS NULL) or platform-admin gated. Idempotent
    // (guarded by `_agentSubTabWired`).
    _initAgentSubTab();

    // GE.MAA.2 (mod-assistant-agent §9.6/9.7) — init the platform-admin
    // defaults block nested inside the Agent sub-tab. Platform-admin only;
    // idempotent (guarded by `_platformAssistantConfigWired`).
    _initPlatformAssistantConfig();

    // GE.AIC.6: the Models sub-tab (self-hosted embedding upload/list, em-*
    // ids) was removed from AI Config — uploaded models still work, existing
    // data is untouched, but the page is gone. See settings.html's
    // #ai-tool-models comment for the full rationale/where each piece moved.

    // ai-config-stack-binding §7.2 — "Who uses what". After the per-tool
    // configs are loaded (its external-endpoint options come from `_aiFormState`)
    // and after §7.1's stacks read (its stack options reuse `_aiStacksLast`).
    // A failed bindings read leaves the last render plus a one-line notice.
    // 2400LR-cdu: bindings + knowledge-sources reads are ai_config*.py, api_allow in CE
    // (open-core/manifest.yaml:801, AA-CE) — the rows paint on CE.
    initAiConfigViewToggle(document, window.localStorage, !!(currentUser && currentUser.is_platform_admin));
    await _loadAiSlotsTable();
    _wireAiSlotsTable();
    // BUG.B59c (owner Q-B59=A) — "Knowledge sources", right after the who-uses-what
    // rows it is styled like, and loaded at the same point: its stack options reuse
    // §7.1's `_aiStacksLast` read and its external-endpoint options come from
    // `_aiFormState`, both of which are populated above. A failed read leaves a
    // one-line notice, never a blank box.
    await _loadAiKnowledgeSources();
    _wireAiKnowledgeSources();
    // The ten per-tool tabs stay; their execution-mode dropdowns do not render
    // (the template ships none — this hides one an older build left in the DOM).
    _hideLegacyExecutionModeSelects();

    // system-managed-ai — the master control. Last, because it reads the
    // per-tool state the calls above have just populated in order to show
    // which selection is currently in effect.
    _initMasterBar();
}

// Uploaded embedding models (name/files/bytes), fetched once per AI-config
// load and shared by the per-tool "Embedding model" dropdown.
let _uploadedEmbeddingModels = [];
// Tools whose config exposes an embedding-model picker (they actually embed:
// wizard retrieval + assistant/RAG). Pure-LLM tools (adapter, repair) don't.
const EMBEDDING_TOOLS = ['wizard', 'assistant'];

async function _loadEmbeddingModelList() {
    const token = sessionStorage.getItem('accessToken');
    if (!token) { _uploadedEmbeddingModels = []; return; }
    try {
        const resp = await fetch(`${window.APP_CONFIG?.API_URL || ''}/embedding-models`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        _uploadedEmbeddingModels = resp.ok ? await resp.json() : [];
    } catch {
        _uploadedEmbeddingModels = [];
    }
}

// ---------------------------------------------------------------------------
// GE.AIC.1-3 — "MOD Stack" as an AI-Configuration provider.
//
// Every model slot (the four tools' primary "Managed model" block, the
// Node Wizard's separate review model, and the three embedding stores) can
// point at a persistent stack instead of a cloud/self-hosted provider.
// `_stackOptions` (one row per stack, tagged kind:"chat"|"embedding") is
// fetched once per page load from GET /platform/ai-config/stack-options and
// shared by every slot's Stack picker. Chat slots filter to kind:"chat",
// embedding slots to kind:"embedding".
// ---------------------------------------------------------------------------
let _stackOptions = [];

async function _loadStackOptions() {
    const token = sessionStorage.getItem('accessToken');
    if (!token) { _stackOptions = []; return; }
    // rc20-aicfg-gate — GET /platform/ai-config/stack-options is routers/ai_config.py:3043
    // (router prefix "/platform", ai_config.py:45). ai_config.py ships in CE: open-core/
    // manifest.yaml:801 lists `CORE/API/routers/ai_config.py` under api_allow (AA-CE comment
    // at :800) and CORE/API/main.py:2343 mounts the router, so the read is live on CE and
    // the three embedding pickers get their Stack options there — no paid bail. The empty
    // list stays the fail-closed state every Stack picker renders when the read fails.
    try {
        const resp = await fetch(`${window.APP_CONFIG?.API_URL || ''}/platform/ai-config/stack-options`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        _stackOptions = resp.ok ? await resp.json() : [];
    } catch {
        _stackOptions = [];
    }
}

// sz-mesh-native.5 — an AI-Config option is labelled by what the stack SERVES
// (its --served-model-name list), not by the container/stack name. The stack
// name is only the fallback when nothing is being served (stopped / idle), so
// an operator who types "granite-3.3-8b" into a node finds the row that says
// "granite-3.3-8b", never a row that only says "llm-big".
function _stackOptionLabel(o) {
    const served = o.served_models || [];
    const stopped = o.status !== 'running';
    if (!served.length) {
        return stopped ? `${o.name} (stopped)` : o.name;
    }
    const names = served[0] + (served.length > 1 ? ` +${served.length - 1} more` : '');
    return stopped ? `${names} (stopped — stack ${o.name})` : names;
}

// Populate a Stack <select> with the stacks of one kind. Stopped stacks stay
// selectable and labelled "(stopped)" — an operator may pin one before
// starting it (matches the backend's validate_stack_choice, which does not
// require status=="running"; only call-time resolve_stack_endpoint does).
function _populateStackSelect(sel, kind, currentValue) {
    if (!sel) return;
    const list = _stackOptions.filter((o) => o.kind === kind);
    sel.innerHTML = '<option value="">Choose a stack…</option>';
    list.forEach((o) => {
        const opt = document.createElement('option');
        opt.value = String(o.stack_id);
        opt.textContent = _stackOptionLabel(o);
        opt.dataset.servedModels = JSON.stringify(o.served_models || []);
        sel.appendChild(opt);
    });
    if (currentValue !== undefined && currentValue !== null && currentValue !== '') {
        sel.value = String(currentValue);
    }
}

// Populate the served-model picker from whichever stack is currently chosen
// in `stackSel`, hiding it (via its `[data-served-model-wrap]` ancestor) when
// the stack serves exactly one model — served_model then saves as "" and the
// API auto-picks the stack's one model.
function _populateServedModelSelect(stackSel, servedSel, currentValue) {
    if (!servedSel) return;
    const chosen = stackSel && stackSel.selectedOptions[0];
    const models = chosen && chosen.value ? JSON.parse(chosen.dataset.servedModels || '[]') : [];
    const wrap = servedSel.closest('[data-served-model-wrap]');
    servedSel.innerHTML = '';
    if (models.length <= 1) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = models.length === 1 ? `Auto (${models[0]})` : 'Auto (this stack serves one model)';
        servedSel.appendChild(opt);
        if (wrap) wrap.classList.add('hidden');
    } else {
        const ph = document.createElement('option');
        ph.value = '';
        ph.textContent = 'Choose a model…';
        servedSel.appendChild(ph);
        models.forEach((m) => {
            const opt = document.createElement('option');
            opt.value = m;
            opt.textContent = m;
            servedSel.appendChild(opt);
        });
        if (wrap) wrap.classList.remove('hidden');
    }
    if (currentValue && models.includes(currentValue)) servedSel.value = currentValue;
}

// Show/hide Base URL, API Key, Compute Target, Keep Alive vs. the Stack /
// served-model pickers, for the primary "Managed model" block AND (wizard
// only) the separate critic/review panel — each block's own Provider select
// drives its own visibility independently, in one call keyed off `form`.
function _applyStackProviderToggle(form) {
    if (!form) return;
    const criticPanel = form.querySelector('[data-critic-panel]');
    const blocks = [
        { root: form, providerSel: form.querySelector('[data-field="provider"]'),
          stackSel: form.querySelector('[data-field="stack_id"]'),
          servedSel: form.querySelector('[data-field="served_model"]'),
          isCritic: false },
    ];
    if (criticPanel) {
        blocks.push({
            root: criticPanel,
            providerSel: criticPanel.querySelector('[data-critic-field="provider"]'),
            stackSel: criticPanel.querySelector('[data-critic-field="stack_id"]'),
            servedSel: criticPanel.querySelector('[data-critic-field="served_model"]'),
            isCritic: true,
        });
    }
    blocks.forEach(({ root, providerSel, stackSel, servedSel, isCritic }) => {
        if (!providerSel) return;
        const isStack = providerSel.value === 'stack';
        root.querySelectorAll('[data-hide-for-stack]').forEach((el) => {
            // A `root===form` pass must skip elements that live inside the
            // critic panel — its own pass (root===criticPanel) owns those.
            if (!isCritic && el.closest('[data-critic-panel]')) return;
            el.classList.toggle('hidden', isStack);
        });
        root.querySelectorAll('[data-show-for-stack]').forEach((el) => {
            if (!isCritic && el.closest('[data-critic-panel]')) return;
            el.classList.toggle('hidden', !isStack);
        });
        if (isStack && stackSel) {
            _populateStackSelect(stackSel, 'chat', stackSel.value);
            if (servedSel) _populateServedModelSelect(stackSel, servedSel, servedSel.value);
        }
    });
}

function _populateEmbeddingSelect(sel, currentValue) {
    if (!sel) return;
    sel.innerHTML = '<option value="">Platform default</option>';
    _uploadedEmbeddingModels.forEach((m) => {
        const opt = document.createElement('option');
        opt.value = m.name;
        opt.textContent = m.name;
        sel.appendChild(opt);
    });
    sel.value = currentValue || '';
}

// Inject the "Embedding model" picker into an embedding-relevant tool form.
// Idempotent per form. Uses data-field="embedding_model" so the existing
// capture/save machinery persists it.
const _SELECT_CLS = 'px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-900 dark:text-white rounded-md w-full text-sm max-w-md';

// An approval-gated embedding-model <select>. Changing it warns that saving
// re-embeds that store, and reverts if the admin declines.
function _makeGatedEmbeddingSelect(tool, dataField, storeLabel, currentValue) {
    const sel = document.createElement('select');
    sel.setAttribute('data-field', dataField);
    sel.className = _SELECT_CLS;
    _populateEmbeddingSelect(sel, currentValue);
    sel.dataset.committed = currentValue || '';
    sel.addEventListener('change', () => {
        const next = sel.value, committed = sel.dataset.committed || '';
        if (next === committed) { _aiFormState[tool][dataField] = next; return; }
        const target = next || 'Platform default';
        const ok = window.confirm(
            `Switch the ${storeLabel} embedding model to "${target}"?\n\n` +
            `On save this starts a RE-EMBED of the ${storeLabel} store (a dimension ` +
            `migration) — seconds to minutes, with stale retrieval results until it ` +
            `finishes.\n\nProceed only if you understand a re-embed will run.`);
        if (!ok) { sel.value = committed; return; }
        sel.dataset.committed = next;
        _aiFormState[tool][dataField] = next;
    });
    return sel;
}

// Inject the embedding-model picker(s). Node Wizard → one dropdown (pgvector
// store). AI Assistant → a store sub-choice (User RAG | MOD docs) + a dropdown
// per store. Idempotent per form.
// A trimmed embedding-model SECTION (matching the redesign's trimmed block).
// GE.AIC.6 (2026-09-29): the Models tab is gone — "we run our models within
// the stacks area" (owner). Platform default stays the default provider
// (managed centrally, no stack of the operator's own to point at); MOD Stack
// points the store at one of the operator's own persistent stacks instead.
// There is no seeded/starter embedding stack today (checked services/
// starter_package.py and the seed templates — none define one), so "Platform
// default" here means whatever the platform's own managed embedding path
// resolves to, not a stack the operator can inspect. Gap: if the product
// wants "Platform default" to name a concrete stack, one needs to be added
// to the starter package first.
// GE.AIC.1-3 — embedding_provider/_stack_id field names are derived from
// dataField itself: embedding_model -> embedding_provider/embedding_stack_id;
// embedding_model_user_rag -> ..._user_rag; embedding_model_docs -> ..._docs.
// When Provider = "MOD Stack", the served model is chosen straight off the
// stack's served_models list, written into dataField itself (the SAME field
// the "Platform default" Model picker uses) — no parallel served-model field
// for embeddings, so the Model <select> never has a second element sharing
// its data-field (the generic _captureFormState walker takes the LAST
// [data-field] match in DOM order; two would silently race).
function _makeEmbeddingSection(tool, dataField, heading, sub, currentValue) {
    const sec = document.createElement('section');
    sec.className = 'rounded-lg border border-gray-200 bg-white p-4 space-y-2';
    const head = document.createElement('div');
    head.innerHTML =
        `<h4 class="text-sm font-semibold text-gray-800">${_esc(heading)}</h4>
         <p class="text-xs text-gray-500">${sub}</p>`;
    sec.appendChild(head);

    const providerField = dataField.replace('embedding_model', 'embedding_provider');
    const stackField = dataField.replace('embedding_model', 'embedding_stack_id');
    const st = _aiFormState[tool] || (_aiFormState[tool] = {});

    const providerLabel = document.createElement('label');
    providerLabel.className = 'block text-sm font-medium text-gray-700';
    providerLabel.textContent = 'Provider';
    sec.appendChild(providerLabel);
    const providerSel = document.createElement('select');
    providerSel.setAttribute('data-field', providerField);
    providerSel.className = _SELECT_CLS;
    providerSel.innerHTML = '<option value="">Platform default</option><option value="stack">MOD Stack</option>';
    providerSel.value = st[providerField] || '';
    sec.appendChild(providerSel);

    const stackWrap = document.createElement('div');
    stackWrap.className = 'space-y-1.5 hidden mt-2';
    stackWrap.dataset.showForStack = '1';
    const stackLabel = document.createElement('label');
    stackLabel.className = 'block text-sm font-medium text-gray-700';
    stackLabel.textContent = 'Stack';
    stackWrap.appendChild(stackLabel);
    const stackSel = document.createElement('select');
    stackSel.setAttribute('data-field', stackField);
    stackSel.className = _SELECT_CLS;
    stackWrap.appendChild(stackSel);
    sec.appendChild(stackWrap);

    const modelLabel = document.createElement('label');
    modelLabel.className = 'block text-sm font-medium text-gray-700 mt-2';
    modelLabel.textContent = 'Model';
    sec.appendChild(modelLabel);
    const modelSel = _makeGatedEmbeddingSelect(tool, dataField, heading, currentValue);
    sec.appendChild(modelSel);

    const note = document.createElement('p');
    note.className = 'text-xs text-gray-500';
    note.innerHTML = 'Provider, base URL and compute are managed &mdash; '
        + '<strong>Platform default</strong> uses the platform\'s managed embedding path, '
        + 'or point this store at one of your own MOD Stacks. '
        + '<span class="text-warning">Changing the model re-embeds this store.</span>';
    sec.appendChild(note);

    // Seeds the Stack picker's initial selection from the persisted config
    // exactly once; afterward stackSel.value (a real DOM change) is used.
    let pendingStackId = st[stackField] || '';
    const toggle = () => {
        const isStack = providerSel.value === 'stack';
        stackWrap.classList.toggle('hidden', !isStack);
        if (isStack) {
            const wantStackId = stackSel.value || pendingStackId;
            _populateStackSelect(stackSel, 'embedding', wantStackId);
            pendingStackId = '';
            const chosen = stackSel.selectedOptions[0];
            const models = chosen && chosen.value ? JSON.parse(chosen.dataset.servedModels || '[]') : [];
            const cur = modelSel.value || currentValue;
            modelSel.innerHTML = '<option value="">Choose a model…</option>';
            models.forEach((m) => {
                const opt = document.createElement('option');
                opt.value = m;
                opt.textContent = m;
                modelSel.appendChild(opt);
            });
            if (cur && models.includes(cur)) modelSel.value = cur;
        } else {
            _populateEmbeddingSelect(modelSel, modelSel.dataset.committed || '');
        }
        modelSel.dataset.committed = modelSel.value;
        st[dataField] = modelSel.value;
    };
    providerSel.addEventListener('change', () => {
        st[providerField] = providerSel.value;
        toggle();
    });
    stackSel.addEventListener('change', () => {
        st[stackField] = stackSel.value;
        toggle();
    });
    if (providerSel.value === 'stack') toggle();

    return sec;
}

function _injectEmbeddingSelect(tool, form, config) {
    if (!EMBEDDING_TOOLS.includes(tool) || !form) return;
    if (form.querySelector('[data-embedding-block]')) return; // already injected
    const wrap = document.createElement('div');
    wrap.dataset.embeddingBlock = '1';
    wrap.className = 'space-y-5';

    if (tool === 'wizard') {
        // Learning-store embedding (trimmed).
        wrap.appendChild(_makeEmbeddingSection(
            'wizard', 'embedding_model', 'Learning-store embedding',
            "The model that embeds the Node Wizard's learning store.",
            config.embedding_model || ''));
    } else { // assistant — two independent stores, each its own trimmed section
        // Preserve the stored store preference; both pins persist regardless.
        _aiFormState.assistant.embedding_store = config.embedding_store || 'user_rag';
        wrap.appendChild(_makeEmbeddingSection(
            'assistant', 'embedding_model_user_rag', 'User-RAG embedding',
            'Embeds the workflow VectorQuery / Upsert store (User RAG).',
            config.embedding_model_user_rag || ''));
        wrap.appendChild(_makeEmbeddingSection(
            'assistant', 'embedding_model_docs', 'MOD-docs embedding',
            'Embeds the MOD documentation store the assistant retrieves help from.',
            config.embedding_model_docs || ''));
    }

    // Insert BEFORE the Save block so the button stays at the bottom.
    const saveBlock = form.querySelector('[data-role="save-block"]');
    if (saveBlock) form.insertBefore(wrap, saveBlock);
    else form.appendChild(wrap);
}

function _anyToolInAdvancedMode() {
    return AI_TOOLS.some((tool) => {
        const st = _aiFormState[tool] || {};
        if (st.execution_mode) return st.execution_mode === 'advanced';
        const sel = document.querySelector(
            `#ai-tool-${tool} form [data-field="execution_mode"]`);
        return sel ? sel.value === 'advanced' : false;
    });
}

// ---------------------------------------------------------------------------
// system-managed-ai — the master control.
//
// AI Configuration opens on ten tabs, each with its own execution mode, model
// choice, tier picker and knobs. An operator who understands the system can
// work through it; anyone else calls someone who does. That is the actual
// failure: the product asks a non-technical user a series of questions only an
// engineer can answer.
//
// The platform already knows the answers. This surfaces TWO controls and
// derives the rest.
// ---------------------------------------------------------------------------

const AI_MASTER_MODE_KEY = 'mod_ai_master_mode';

/** Which tabs belong to Advanced. In System managed the platform picks these,
 *  so showing them is showing knobs that do nothing.
 *
 *  GE.AIC.5/6 (2026-09-29): 'gpu' and 'models' both removed from AI Config
 *  entirely (their tab buttons/panels no longer exist) — see the settings.html
 *  tab-bar comments for where each moved. */
const _ADVANCED_ONLY_TABS = [
    // The four AI tools are now each their own advanced-only tab (tools first).
    'wizard', 'adapter', 'assistant', 'repair',
    'agent', 'scheduler', 'training',
    'wizard-learning', 'resource-monitor',
];

function _budgetLabel(tier) {
    // The hardware SHAPE, not just the total. The generator and reasoner are
    // placed independently and may land on different cards, so "45 GB" could
    // mean 45 on one card or 22.5+22.5 on two — very different requirements
    // behind the same number. Someone with two small cards would otherwise
    // pick a total they can afford and get a configuration that cannot place.
    const total = tier.total_demand_gb || 0;
    const cards = tier.cards_required || 0;
    const roles = (tier.stacks || []).filter((r) => (r.vram_floor_gb || 0) > 0);

    if (total <= 0 || cards === 0) return 'No GPU — runs on CPU only';

    // The breakdown names the SERVED MODELS, not the internal roles — the
    // operator sees "qwen3-8b-q4 15 + gpt-oss-20b 22.5", never "generator 15".
    const breakdown = roles.length > 1
        ? ' (' + roles.map((r) => `${r.model || '?'} ${r.vram_floor_gb}`).join(' + ') + ')'
        : '';
    // vram_floor_gb is the CARD size the rung is specified against, so the
    // label states real hardware rather than rounding the demand up into a
    // card size that may not exist. 14.5 GB of demand is "a 16 GB card",
    // not "a 15 GB card".
    const card = tier.vram_floor_gb || Math.ceil(total);
    const shape = cards > 1
        ? `needs ${cards} cards of ${card} GB`
        : `fits one ${card} GB card`;
    // "reserved", NOT "total". Every figure here is a per-role vram_floor_gb,
    // which is a placement guard padded ~0.8 GB above what the model actually
    // takes so a stack never lands where it would crash. Summing floors and
    // calling the result a total overstates real use — the compact pair
    // reserves 15.5 GB and measures 14.25 GB resident — which reads as "this
    // barely fits" on a card with over a gigabyte spare.
    return `${total} GB reserved${breakdown} — ${shape}`;
}

// ai-config-stack-binding §7.3b — the tier dropdown is GONE from this page.
// Model / tier / placement are now chosen on Persistent Stacks ("New AI stack",
// built in persistent-stacks.js), so the master bar no longer asks the operator
// to pick a rung and press Apply. What it does instead is state, in prose, what
// a slot set to "Let MOD choose" will get: the tier the platform resolved, the
// models that tier serves, and the placement verdict for this fleet.
//
// The name stays `_renderBudgetOptions` because it is the one entry point the
// master-bar loader calls; only its OUTPUT changed (options → sentences). The
// ladder itself is untouched, so §7.4's `newly_available` badge data still
// arrives and is simply not rendered as a selectable rung.
function _renderBudgetOptions(ladder, current) {
    _renderTierReading(current);
}

// The read-only tier reading. `current` is the tier the platform resolved
// (stored setting → env → default, echoed by GET /platform/ai-config/tiers);
// falling back to the ladder's own default keeps the sentence truthful when the
// resolver has not answered yet.
// Two elements, because the two halves answer different questions:
// `ai-master-budget-reading` states what "Let MOD choose" buys and where to go
// to pick something else; `ai-master-budget-help` carries the placement verdict
// for THIS fleet, which is the part that changes as hardware is added.
function _renderTierReading(preset) {
    const reading = document.getElementById('ai-master-budget-reading');
    const help = document.getElementById('ai-master-budget-help');
    if (!reading || !help) return;
    const tier = (preset && _tier(preset))
        || (_managedAiSetting && _managedAiSetting.tier
            && _tier(_managedAiSetting.tier))
        || ((_tierLadder && _tierLadder.ladder) || [])[0];
    if (!tier) {
        reading.textContent = '';
        help.innerHTML = '<span class="text-gray-500">Tier ladder unavailable.</span>';
        return;
    }
    const roles = (tier.stacks || []).filter((r) => r.model);
    const models = roles.map((r) => r.model).join(' · ');
    const pl = tier.placement || {};
    const unplaceable = pl.placeable === false;
    reading.innerHTML =
        `<div class="font-medium">When a slot is set to &ldquo;Let MOD choose&rdquo;, MOD uses `
        + `${_esc(tier.label || tier.preset)} &mdash; ${_esc(models || 'no models')} `
        + `&mdash; on ${_esc(tier.hardware_requirement || 'the accelerator this fleet has')}.</div>`
        + `<div class="mt-1 text-gray-500">To run a different model, use `
        + `<a href="#" data-ps-new-stack="1" class="underline text-primary">`
        + `Persistent Stacks &rarr; New AI stack</a>.</div>`;
    help.innerHTML = unplaceable
        ? `<span class="text-destructive">${_esc(pl.reason
            || 'No worker in this fleet can host this tier.')}</span>`
        : '<span class="text-success">&check; This fleet can host it</span>';
}

// §7.3b — there is no tier dropdown left on this page, so the reading is keyed
// to what the platform actually resolved (GET /platform/managed-ai/setting),
// not to a selection the operator made here. Same sentences as
// _renderTierReading; kept as its own function because the master-bar loader
// and the status poll both refresh it independently of the ladder render.
function _updateBudgetHelp() {
    _renderTierReading((_managedAiSetting && _managedAiSetting.tier)
        || (_aiFormState.wizard || {}).local_preset
        || (_tierLadder && _tierLadder.default));
}

// ---------------------------------------------------------------------------
// managed-ai-self-deploy §5 — the tier is a setting now (§1), and Apply has a
// preview (§3). These wire the master bar to both: an accelerator dropdown
// alongside the tier, a plan revealed by CHANGING a selection (never by
// loading the page), and a distinct way to abandon a pending change.
// ---------------------------------------------------------------------------

// What GET /platform/managed-ai/setting last reported: the tier + accelerator
// actually in effect, and which source decided each. "Abandon" resets the
// dropdowns to this, not to whatever they last held.
let _managedAiSetting = { tier: null, tier_source: null,
                          accelerator_kind: null, accelerator_kind_source: null };

// The most recently fetched plan, so Apply's success/failure handling can
// tell whether the change it is about to make matches what was last
// previewed (best-effort; Apply always re-derives on the server, this is
// only for the UI's own bookkeeping).
let _managedAiPlan = null;

async function _fetchManagedAiSetting() {
    // rc20-s20 — /platform/managed-ai/setting is served by routers/ai_config.py
    // (:2660), which open-core/manifest.yaml denies on CE (the same denial
    // loadAIConfig already fail-opens on at :3347 _ceNoAiCfg), so the read
    // 404s there and the master bar keeps its ladder default. Never fire the
    // fetch on CE; paid behaviour unchanged (fail-open on unreadable edition).
    if (!await isPaidEdition()) return _managedAiSetting;
    try {
        _managedAiSetting = await _masterFetch('/platform/managed-ai/setting');
    } catch (_) { /* best-effort — the bar still works off the ladder default */ }
    return _managedAiSetting;
}

// §5.3 — a selection differs from what is actually running whenever the tier
// has moved off the resolved setting. That is the one fact that decides
// whether "Abandon" is offered at all. (The accelerator vendor is no longer a
// user choice — the backend resolves the kind itself — so only the tier is
// compared here.)
// §7.3b — there is no tier selection left on this page, so nothing here can
// differ from what is running: the tier is whatever the platform resolved, and
// the only way to change it is "New AI stack" on Persistent Stacks. Kept as a
// predicate because the abandon-visibility refresh still runs on every poll.
function _masterSelectionDiffersFromEffective() {
    return false;
}

function _updateAbandonVisibility() {
    const btn = document.getElementById('ai-master-abandon');
    if (!btn) return;
    // §7.3b — "Abandon" existed to undo a tier picked on THIS page. The page
    // picks no tier any more, so the button is never offered.
    btn.classList.add('hidden');
}

// §7.3b — `_refreshManagedAiPlan` and `_abandonMasterSelection` are GONE. Both
// existed to serve a tier picked on THIS page: one previewed what Apply would
// deploy, the other undid the pick. This page picks no tier, so neither action
// has a subject. The stack-level plan is still shown where it matters —
// `_previewSystemManagedSwitch` (below) previews a MODE switch, which is still
// a decision this page makes — and the deploy itself now happens on Persistent
// Stacks, where that page draws its own plan.

// system-managed-ai §3.3 — preview a MODE switch before applying, using the
// same plan-then-apply shape a tier change uses. Toggling to System managed
// shows what it WILL do — the stack actions (plan) and the per-tool bindings
// (derived) — without deploying anything; Apply stays the explicit commit and
// toggling back to Advanced (or Abandon) leaves everything untouched, since the
// switch only stashed config and rendered a preview.
async function _previewSystemManagedSwitch() {
    const panel = document.getElementById('ai-master-plan');
    if (!panel) return;
    // §7.3b — the tier a switch would apply is no longer a selection made here
    // (the tier dropdown is gone): it is whatever the platform resolved, else
    // the ladder default.
    const preset = (_managedAiSetting && _managedAiSetting.tier)
        || (_tierLadder && _tierLadder.default);
    if (!preset) return;

    panel.classList.remove('hidden');
    panel.innerHTML = '<span class="text-gray-500">Previewing System managed…</span>';
    try {
        const qs = new URLSearchParams({ preset });
        const plan = await _masterFetch(`/platform/managed-ai/plan?${qs.toString()}`);
        _managedAiPlan = plan;
        let html = _renderManagedAiPlanHtml(
            plan, 'Switching to System managed &mdash; preview, not yet applied');
        try {
            const derived = await _masterFetch(`/platform/managed-ai/derived?${qs.toString()}`);
            html += _renderDerivedHtml(derived);
        } catch (_) { /* best-effort — the plan above stands on its own */ }
        html += '<div class="mt-2 text-gray-500">Nothing is applied yet. '
            + '<strong>Apply</strong> commits this; switching back to Advanced '
            + 'leaves your current setup untouched.</div>';
        panel.innerHTML = html;
    } catch (e) {
        panel.innerHTML = `<span class="text-destructive">Could not preview: ${_esc(e.message || String(e))}</span>`;
    }
}

// §4.6/§7.5 — the per-tool derivation, inspectable. A reasoning tool a tier
// cannot serve is shown unresolved WITH its reason rather than dropped, because
// a missing row reads as "fine" and a silent generator answer to a reasoning
// request is the downgrade nobody consented to.
function _renderDerivedHtml(derived) {
    const tools = (derived && derived.tools) || {};
    const names = Object.keys(tools);
    if (!names.length) return '';
    // Rows name the SERVED MODEL, not the internal role (mesh-native-llm-routing
    // §4.6): "assistant → gpt-oss-20b", never "assistant (reasoner) → …".
    const rows = names.map((name) => {
        const t = tools[name];
        if (t.resolved) {
            return `<div class="text-gray-700"><strong>${_esc(name)}</strong> `
                + `&rarr; ${_esc(t.model || 'no model')}</div>`;
        }
        return `<div class="text-warning"><strong>${_esc(name)}</strong> `
            + `&mdash; ${_esc(t.reason || 'not served by this tier')}</div>`;
    }).join('');
    const warn = derived.fully_resolved === false
        ? `<div class="text-warning mt-1">Some tools are not served by this budget — they will not work until you pick a budget that provides a model for them.</div>`
        : '';
    // §6.3 — the settings the budget does NOT determine, shown as platform
    // defaults and visibly separated from the budget-derived rows above so a
    // poor default is attributed to the platform opinion, not the dropdown.
    const defaults = (derived && derived.platform_defaults) || {};
    const defNames = Object.keys(defaults);
    let defBlock = '';
    if (defNames.length) {
        const defRows = defNames.map((k) => {
            const v = defaults[k] || {};
            const val = v.policy || v.autonomy
                || (v.enabled === false ? 'off' : v.enabled === true ? 'on' : '');
            return `<div class="text-gray-600"><strong>${_esc(k)}</strong>`
                + (val ? ` &rarr; ${_esc(String(val))}` : '')
                + (v.note ? ` <span class="text-gray-400">— ${_esc(v.note)}</span>` : '')
                + `</div>`;
        }).join('');
        defBlock = `<div class="mt-2 pt-2 border-t border-dashed border-gray-200">`
            + `<div class="font-medium text-gray-800 mb-1">Platform defaults `
            + `<span class="text-gray-400">(not from your budget)</span></div>`
            + defRows + `</div>`;
    }
    return `<div class="mt-3 pt-2 border-t border-gray-200">`
        + `<div class="font-medium text-gray-800 mb-1">Each tool will use</div>`
        + rows + warn + defBlock + `</div>`;
}

function _renderManagedAiPlanHtml(plan, heading) {
    const rows = (plan.actions || []).map((a) => {
        const color = a.action === 'deploy' ? 'text-primary'
            : a.action === 'stop' ? 'text-destructive' : 'text-gray-500';
        const verb = a.action === 'deploy' ? 'Deploy' : a.action === 'stop' ? 'Stop' : 'Leave as-is';
        // Subject is the SERVED MODEL, never the role word or the internal
        // stack id (mesh-native-llm-routing §4.6); the id stays as the title.
        const subject = a.target_model || 'Managed AI model';
        const models = a.action === 'deploy' && a.current_model && a.target_model
                       && a.current_model !== a.target_model
            ? ` (${_esc(a.current_model)} &rarr; ${_esc(a.target_model)})`
            : '';
        return `<div class="${color}" title="${_esc(a.stack)}"><strong>${verb}</strong> ${_esc(subject)}${models} `
            + `<span class="text-gray-500">&mdash; ${_esc(a.reason)}</span></div>`;
    }).join('');

    const dl = plan.download_estimate || {};
    const mb = dl.upper_bound_bytes ? Math.round(dl.upper_bound_bytes / 1048576) : 0;
    const dlLine = mb > 0
        ? `<div class="mt-2 text-gray-600">Up to ~${mb.toLocaleString()} MB may download `
          + `(upper bound${dl.unknown ? ', at least — some sizes are unknown' : ''}). `
          + `${_esc(dl.note || '')}</div>`
        : '';

    const p = plan.placement;
    let placementLine = '';
    if (p && p.placeable === false) {
        const fix = p.fits_instead
            ? ` The <strong>${_esc(p.fits_instead.preset)}</strong> tier ${_esc(p.fits_instead.reason)}.`
            : '';
        placementLine = `<div class="mt-2 text-red-700"><strong>Will not fit this fleet:</strong> `
            + `${_esc(p.reason || 'no worker can host it')}.${fix}</div>`;
    }

    const readonlyLine = plan.stack_path_enabled === false
        ? `<div class="mt-2 text-amber-800">${_esc(plan.message)}</div>` : '';

    const head = heading || 'Pending change &mdash; not yet applied';
    return `<div class="font-medium text-gray-800 mb-1">${head}</div>`
        + (rows || '<div class="text-gray-500">Nothing to do.</div>')
        + dlLine + placementLine + readonlyLine;
}

// §5.3 — abandon a pending change: reset both dropdowns to what is actually
// §7.3b — `_abandonMasterSelection` is GONE: it existed to undo a tier picked
// on THIS page, and this page picks no tier. The mode switch keeps its own
// escape (switching back to Advanced), which needs no undo verb because
// changing a selection never deployed anything.

// §5.6/§5.7 — read-only when the compose path is in use: the API has no
// docker socket, so there is nothing here for Apply to act on, and the
// `docker compose` remedy is the honest instruction ONLY on that path.
// FIX-AICONFIG-IDLE-APPLY-CE — cached edition verdict for the SYNC visibility
// helpers below. `_applyMasterMode` and `_applyMasterReadOnlyGate` cannot await
// `isPaidEdition`, so the loaders below refresh this cache before they run.
// Fail closed: an unknown/false edition HIDES the residency toggle, because an
// Apply that cannot reach the wholly-Paid llm-idle route must not offer a
// control whose effect silently does not happen.
let _paidEditionKnown = false;

// §7.3b — the gate no longer disables a tier dropdown: there is none. It still
// disables Apply, because on the compose path the API has no docker socket to
// act through even for the residency save, and it still explains the remedy.
function _applyMasterReadOnlyGate() {
    const box = document.getElementById('ai-master-readonly');
    const applyBtn = document.getElementById('ai-master-apply');
    const onComposePath = !!(_managedAiState && _managedAiState.enabled === false);

    if (applyBtn) applyBtn.disabled = onComposePath;

    // FIX-AICONFIG-IDLE-APPLY-CE — CE hides the "When idle" residency toggle:
    // its Apply writes the wholly-Paid /stacks/llm-idle/settings route, which
    // 402s here, so the control could only ever half-apply and then report a
    // Pro-feature error. The markup stays (settings.html untouched) — hidden
    // via classList, the same pattern `_applyMasterMode` already uses for
    // `ai-master-warm-wrap`. Runs BEFORE the early returns below, so a page
    // that never reaches the compose-path branch still hides it.
    if (!_paidEditionKnown) {
        document.getElementById('ai-master-warm-wrap')?.classList.add('hidden');
    }

    if (!box) return;
    if (!onComposePath) { box.classList.add('hidden'); box.innerHTML = ''; return; }
    box.classList.remove('hidden');
    box.innerHTML = '<strong>Read-only:</strong> managed AI runs as a compose service on this '
        + `deployment (<code>${_esc((_managedAiState && _managedAiState.flag) || 'MANAGED_AI_AS_STACKS')}</code> `
        + 'is not set), and the API has no docker socket, so it cannot change what runs here. '
        + `<div class="mt-1">The models themselves are chosen on `
        + `<a href="#" data-ps-new-stack="1" class="underline text-primary">Persistent Stacks &rarr; New AI stack</a>, `
        + `which this page only reports on. To have this page manage those stacks instead, set `
        + `<code>${_esc((_managedAiState && _managedAiState.flag) || 'MANAGED_AI_AS_STACKS')}=true</code> and restart.</div>`;
}

async function _loadMasterBar() {
    const modeSel = document.getElementById('ai-master-mode');
    if (!modeSel) return;

    // An operator who picked a mode while the config was still loading keeps
    // it: the change handler is wired before the fetches, and this late init
    // must not put the select back to the stored value underneath them.
    const mode = modeSel.dataset.userSet
        ? modeSel.value
        : (localStorage.getItem(AI_MASTER_MODE_KEY)
           || (_anyToolInAdvancedMode() ? 'advanced' : 'managed'));
    modeSel.value = mode;

    // FIX-AICONFIG-IDLE-APPLY-CE — cached edition verdict for the SYNC
    // visibility helpers (_applyMasterMode / _applyMasterReadOnlyGate) below,
    // which cannot await. isPaidEdition is served from api.js's cached
    // getPlatformFeatures, so this adds no round trip once the page has loaded,
    // and it never throws (its catch returns false → fail closed, hidden).
    const _paidEdition = await isPaidEdition();
    _paidEditionKnown = _paidEdition;

    // Apply visibility IMMEDIATELY from the already-known mode. The tab strip is
    // hidden by default (settings.html), so applying here — before the several-
    // second idle-settings fetch below — means Managed shows nothing extra and
    // Advanced reveals its tabs at once, instead of flashing the full strip then
    // pulling it back once the network settles. Re-applied at the end too.
    // Skipped once the operator has chosen a mode: the change handler already
    // applied it, and re-applying hides whichever tool panel they opened since.
    const userChose = !!modeSel.dataset.userSet;
    if (!userChose) _applyMasterMode(mode);

    // The ladder is already loaded by loadAIConfig into _tierLadder —
    // refetching here would be a second source of truth for the same data.
    const ladder = (_tierLadder && _tierLadder.ladder) || [];
    // The figures ARE the fleet's own — they are computed per accelerator kind
    // (45 GB on NVIDIA, 42 on AMD, because a different engine serves the same
    // rung). Saying so in the UI was noise: this page only ever shows one
    // fleet's numbers, so the caveat answered a question nobody was asking.
    // The per-vendor correctness is covered by tests, not by a label.
    const kindLabel = document.getElementById('ai-master-kind');
    if (kindLabel) kindLabel.textContent = '';

    // managed-ai-self-deploy §1/§5.1 — what is ACTUALLY in effect, resolved
    // server-side (stored setting -> env -> default). `_tierLadder.default`
    // already carries this (GET /platform/ai-config/tiers echoes the same
    // resolver). The accelerator kind is resolved server-side too and is no
    // longer a user choice, so nothing here reads or renders it.
    await _fetchManagedAiSetting();

    let cur = _tierLadder.default;
    if (ladder.length) {
        // §7.3b — the reading is keyed to what the platform RESOLVED (stored
        // setting → env → default), not to a per-tool `local_preset`: a per-tool
        // tier is an Advanced-tab choice, and "Let MOD choose" ignores it. The
        // resolver already answered above (_fetchManagedAiSetting), so this
        // reads the same source the tier sentence is about.
        cur = (_managedAiSetting && _managedAiSetting.tier) || _tierLadder.default;
        _renderBudgetOptions(ladder, cur);
    } else {
        const reading = document.getElementById('ai-master-budget-reading');
        const help = document.getElementById('ai-master-budget-help');
        if (reading) reading.textContent = '';
        if (help) help.textContent = 'Tier ladder unavailable.';
    }

    // FIX-AICONFIG-IDLE-APPLY-CE — the residency toggle's backing route
    // (/stacks/llm-idle/settings) is wholly Paid (open-core/manifest.yaml:928
    // HARD RULE: the route stays DEFINED and 402s "pro_feature" when the paid
    // module is absent, NO predictive stack system in CE). So on CE this GET
    // could only ever 402 and leave the toggle at its HTML DEFAULT while the
    // bar still rendered — a control whose Apply cannot take effect. Skip the
    // fetch and hide the wrap instead; the markup itself is untouched.
    if (!_paidEdition) {
        document.getElementById('ai-master-warm-wrap')?.classList.add('hidden');
        _updateResidencyEffect();
    } else try {
        const idle = await _masterFetch('/stacks/llm-idle/settings');
        const warm = document.getElementById('ai-master-warm');
        // Prewarm COLLAPSES the two mechanisms that already exist rather than
        // becoming a third beside them: keep-loaded means idle reclaim off.
        // §7.3b — this toggle STAYS: it picks neither a model, a tier, nor a
        // placement, it only says whether the models that ARE running stay
        // loaded, so it is untouched by the model/tier move.
        if (warm) warm.checked = !(idle.platform || {}).enabled;
        _updateResidencyEffect();
    } catch (_) { /* idle settings are optional; the bar still works */ }

    // _managedAiState (stack-path enabled / compose path) is populated by
    // _refreshManagedAiState, called from loadAIConfig alongside this — by
    // the time a user could act on the bar it has run at least once.
    _applyMasterReadOnlyGate();
    _updateAbandonVisibility();

    // The fetches above take seconds. If the operator switched mode meanwhile,
    // the change handler already applied it — re-applying here would hide the
    // tabs or the tool panel they just opened.
    if (!userChose && !modeSel.dataset.userSet) _applyMasterMode(mode);
}

// system-managed-ai §5.4 — state the CURRENT residency and attribute the held
// accelerator capacity to the operator's own prewarm choice, so "why is my card
// busy" answers itself with their decision rather than a mystery queue. This is
// a display hint on the single prewarm toggle — NOT a third residency control
// (§5.2: the toggle already collapses idle-reclaim + pin-warm into one).
function _updateResidencyEffect() {
    const warm = document.getElementById('ai-master-warm');
    const line = document.getElementById('ai-master-warm-effect');
    if (!warm || !line) return;
    line.textContent = warm.checked
        ? "Models stay loaded — this holds accelerator capacity that node tasks "
          + "can't use (on a single-card box, the whole card)."
        : 'Models release when idle — the first call after idle pays a cold start.';
}

function _applyMasterMode(mode) {
    // FIX-DASH-SETTINGS-AI-MASTER-BAR-CE — CE ai-config is ADVANCED-ONLY: the
    // System-managed master bar is a paid surface (api.js: settings AI config),
    // so the effective mode is forced to advanced unless the edition is KNOWN
    // paid (`_paidEditionKnown`, fail closed). Paid keeps `mode !== 'advanced'`.
    const managed = _paidEditionKnown ? (mode !== 'advanced') : false;
    document.getElementById('ai-master-bar')?.classList.toggle('hidden', !_paidEditionKnown);
    document.getElementById('ai-intro-managed')?.classList.toggle('hidden', !managed || !_paidEditionKnown);
    document.getElementById('ai-intro-advanced')?.classList.toggle('hidden', !!managed && _paidEditionKnown);
    document.getElementById('ai-master-budget-wrap')
        ?.classList.toggle('hidden', !managed);
    // FIX-AICONFIG-IDLE-APPLY-CE — the residency wrap is hidden in CE too, not
    // only in Advanced: its Apply writes the wholly-Paid /stacks/llm-idle/
    // settings route, which 402s on CE, so a visible toggle could only half-
    // apply and then report a Pro-feature error. `_paidEditionKnown` is the
    // cached verdict refreshed by the loaders (fail closed: unknown = hidden).
    document.getElementById('ai-master-warm-wrap')
        ?.classList.toggle('hidden', !managed || !_paidEditionKnown);
    document.getElementById('ai-master-advanced-note')
        ?.classList.toggle('hidden', managed);

    // Per-tool controls the master bar now owns. Left visible in System
    // managed they are a second control for a decision already made, and two
    // controls for one decision is how they end up disagreeing about what
    // was actually applied.
    document.querySelectorAll('[data-advanced-only]').forEach((el) => {
        el.classList.toggle('hidden', managed);
    });

    // ai-configuration-page redesign — the four AI tools are now tabs in the
    // strip (tools first), alongside the infrastructure tabs. The strip belongs
    // to Advanced; System managed hides it and every panel entirely.
    document.getElementById('ai-tool-tabbar')?.classList.toggle('hidden', managed);
    document.querySelectorAll('.ai-tool-panel').forEach((el) => {
        el.classList.add('hidden');
    });
    const modeHelp = document.getElementById('ai-master-mode-help');
    if (modeHelp) {
        modeHelp.textContent = managed
            ? 'MOD picks the models and points every tool at them.'
            : 'Choose a model per tool yourself. Every tab is shown.';
    }
    // The hardware tier stays READABLE in managed mode — the user chose a
    // budget and should see what it bought — but not editable, because the
    // master bar is where that choice is made.
    document.querySelectorAll('.ai-tool-form [data-field="local_preset"], '
        + '.ai-tool-form [data-field="local_role"]').forEach((el) => {
        el.disabled = managed;
        el.classList.toggle('bg-gray-100', managed);
        el.title = managed
            ? 'Set by the System managed selection above.' : '';
    });

    _ADVANCED_ONLY_TABS.forEach((tool) => {
        const btn = document.querySelector(`.ai-tool-tab[data-tool="${tool}"]`)
            || document.getElementById(`ai-tool-tab-${tool}`);
        if (!btn) return;
        if (managed) {
            btn.classList.add('hidden');
        } else if (!btn.dataset.adminOnly && !btn.dataset.deliberateHide
            && (tool !== 'resource-monitor' || _paidEditionKnown)) {
            // Admin-gated tabs are revealed by their own init; this must not
            // un-hide a tab the role check deliberately hid.
            btn.classList.remove('hidden');
        }
    });

    // Never leave an empty bordered strip: if the role check hid every tab, a
    // user could see an empty bordered strip. Hide it unless something shows.
    if (!managed) {
        const tb = document.getElementById('ai-tool-tabbar');
        const anyTab = tb && tb.querySelector('.ai-tool-tab:not(.hidden)');
        tb?.classList.toggle('hidden', !anyTab);
        // Default to the first visible tab (Node Wizard) so a panel is shown
        // rather than a bare tab strip over empty space.
        if (anyTab && !document.querySelector('.ai-tool-panel:not(.hidden)')) {
            const first = tb.querySelector('.ai-tool-tab:not(.hidden)');
            if (first) _switchTab(first.dataset.tool);
        }
    }

    localStorage.setItem(AI_MASTER_MODE_KEY, mode);
}

// §7.3b — Apply no longer deploys a tier. Model / tier / placement moved to
// Persistent Stacks → "New AI stack", so the only thing left on this page that
// Apply can act on is the residency toggle (whether the models that ARE
// running stay loaded). The per-tool `local_preset` PUTs, the
// /stacks/managed-ai/apply redeploy, and the stored-setting PUT are gone:
// nothing here chooses a tier any more, so nothing here may write one.
async function _applyMasterSelection() {
    const btn = document.getElementById('ai-master-apply');
    const status = document.getElementById('ai-master-status');
    const warm = document.getElementById('ai-master-warm')?.checked;
    const managed = document.getElementById('ai-master-mode')?.value !== 'advanced';

    if (btn) { btn.disabled = true; btn.textContent = 'Applying…'; }
    if (status) { status.className = 'text-xs mt-3 text-primary'; status.textContent = 'Applying…'; }

    try {
        // FIX-AICONFIG-IDLE-APPLY-CE — the residency save's route is wholly
        // Paid (manifest:928 HARD RULE: DEFINED + 402 "pro_feature" on CE), so
        // on CE this PUT could only 402 AFTER the API half-wrote the platform
        // row, and the user saw `Could not apply: Pro feature: ...`. CE skips
        // the PUT entirely and reports the save in the CE wording — no
        // "Could not apply:" prefix, because nothing failed: there is nothing
        // here to apply. The paid path below is unchanged.
        const _paid = await isPaidEdition();
        if (managed && _paid) {
            await _masterFetch(`/stacks/llm-idle/settings`, {
                method: 'PUT',
                body: JSON.stringify({ enabled: !warm }),
            });
        }
        if (status) {
            status.className = 'text-xs mt-3 text-foreground';
            if (managed && !_paid) {
                status.textContent =
                    'Saved. Residency is platform-managed in MOD Community Edition; '
                    + 'models are chosen on Persistent Stacks.';
            } else {
                status.textContent = managed
                    ? `Saved. Models ${warm ? 'stay loaded' : 'release when idle'}. `
                      + 'The models themselves are chosen on Persistent Stacks.'
                    : 'Advanced mode — per-tool settings are yours to set.';
            }
        }
        await loadAIConfig().catch(() => {});
    } catch (e) {
        if (status) {
            status.className = 'text-xs mt-3 text-destructive';
            status.textContent = `Could not apply: ${e.message || e}`;
        }
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Apply'; }
    }
}

let _masterBarWired = false;
function _initMasterBar() {
    _wireMasterBar();
    _loadMasterBar().catch((e) => console.error('master bar init failed:', e));
}

// Wired at the top of loadAIConfig, before its fetches, so a mode change made
// while the page is still loading is honoured instead of silently dropped.
function _wireMasterBar() {
    if (!_masterBarWired) {
        _masterBarWired = true;
        // FIX-AICONFIG-IDLE-APPLY-CE — warm the cached edition verdict for the
        // sync visibility helpers. `_wireMasterBar` is sync, so this is a
        // promise callback, not an await; the mode `change` handler below also
        // refreshes the cache itself before it re-applies the mode, so a
        // change made before this resolves still gets the right verdict.
        // `isPaidEdition` never throws (its catch returns false = hidden) and
        // getPlatformFeatures is cached in api.js, so this adds no round trip.
        isPaidEdition().then((v) => { _paidEditionKnown = v; });
        document.getElementById('ai-master-mode')
            ?.addEventListener('change', async (e) => {
                e.target.dataset.userSet = '1';
                const newMode = e.target.value;
                // The mode BEFORE this change — _applyMasterMode is what writes
                // the new value, so read the stored one first.
                const prevMode = localStorage.getItem(AI_MASTER_MODE_KEY)
                    || (_anyToolInAdvancedMode() ? 'advanced' : 'managed');
                _applyMasterMode(newMode);
                // 2400CE-a (2026-10-31): the stash/restore below and the §3.3
                // preview further down are all ai_config-router routes
                // (CORE/API/routers/ai_config.py:2955 stash, :2984 restore,
                // :2792 plan, :2921 derived). That router is CE-denied
                // (open-core/manifest.yaml:1533 `CORE/API/**/ai_config*.py`) and
                // therefore unmounted on CE (CORE/API/main.py:2366
                // _CLOUD_PAID_ROUTER_MOUNTS) — so on CE these calls 404, the
                // catch's console.error trips the net-guard, and the user could
                // never reach the CE-open Scheduler sub-tab
                // (scheduler_runtime_config.py, manifest:206 under api_allow).
                // Gate them with the SAME fail-open predicate loadAIConfig already
                // uses for its reads (settings.js:3347 `_ceNoAiCfg = !(await
                // isPaidEdition())`); isPaidEdition is served from api.js's cached
                // getPlatformFeatures, so this adds no round trip once the page
                // has loaded, and it never throws (its catch returns true).
                // _applyMasterMode ran ABOVE this await, so the LOCAL mode
                // transition — which is what shows the Advanced tab strip and its
                // sub-tabs — still happens on CE.
                const _paidNow = await isPaidEdition();
                // FIX-AICONFIG-IDLE-APPLY-CE — refresh the cached verdict the
                // sync helpers use, then re-apply the residency wrap's
                // visibility: `_applyMasterMode` ran ABOVE this await (so the
                // local mode transition still happens on CE), and if this is
                // the first check it ran on the pre-fetch cache, which fails
                // closed to hidden. On paid + managed the wrap must show.
                const _wasKnownPaid = _paidEditionKnown;
                _paidEditionKnown = _paidNow;
                // FIX-DASH-SETTINGS-AI-MASTER-BAR-CE — order risk: the sync apply
                // above ran on the not-yet-warmed fail-closed flag (forced
                // advanced, bar hidden). On paid, re-apply ONCE now it resolved.
                if (_paidNow && !_wasKnownPaid) _applyMasterMode(newMode);
                if (_paidNow && newMode !== 'advanced') {
                    document.getElementById('ai-master-warm-wrap')
                        ?.classList.remove('hidden');
                }
                const _ceNoManagedAi = !_paidNow;
                // §3.2 — preserve the operator's Advanced config across the
                // switch, durably. Stash on the way IN (before Apply overwrites
                // the tools with managed local), restore on the way BACK. Only
                // on a real transition, never on a same-mode re-render.
                try {
                    if (!_ceNoManagedAi && newMode !== 'advanced' && prevMode === 'advanced') {
                        await _masterFetch('/platform/managed-ai/advanced-config/stash',
                                           { method: 'POST' });
                    } else if (!_ceNoManagedAi && newMode === 'advanced' && prevMode !== 'advanced') {
                        const r = await _masterFetch(
                            '/platform/managed-ai/advanced-config/restore', { method: 'POST' });
                        // Reload the per-tool forms so the restored values are
                        // what the operator sees, not the managed-local state
                        // that was on screen a moment ago.
                        if (r && r.changed) await loadAIConfig();
                    }
                } catch (_) { /* best-effort — the mode toggle itself still worked */ }
                // §3.3 — preview the switch before Apply. Entering System
                // managed shows what it WILL do (nothing applied yet); leaving
                // it clears the preview, since Advanced deploys nothing on its
                // own. Independent of the stash/restore above, so a stash
                // hiccup still lets the operator see the preview.
                if (!_ceNoManagedAi && newMode !== 'advanced') {
                    _previewSystemManagedSwitch();
                } else {
                    const panel = document.getElementById('ai-master-plan');
                    if (panel) { panel.classList.add('hidden'); panel.innerHTML = ''; }
                }
            });
        // §7.3b — no tier dropdown and no "Abandon" left here: model / tier /
        // placement are chosen on Persistent Stacks ("New AI stack"), so the
        // change handler that used to turn a selection into a plan is gone and
        // the tier reading is refreshed by the loader + status poll instead.
        // The "When idle" toggle stays (it picks no model, tier, or placement),
        // and Apply now persists that toggle alone.
        document.getElementById('ai-master-warm')
            ?.addEventListener('change', () => { _updateResidencyEffect(); });
        // §7.3b — the tier reading's "Persistent Stacks → New AI stack" link.
        // Same deep link library.js / _gotoPersistentStackRow use (?tab=stacks
        // #system); no stack id, so it lands on the page, where the "New AI
        // stack" action lives beside "Create from compose".
        document.getElementById('ai-master-budget-wrap')
            ?.addEventListener('click', (e) => {
                const link = e.target.closest('a[data-ps-new-stack]');
                if (!link) return;
                e.preventDefault();
                _gotoPersistentStackRow(null);
            });
        document.getElementById('ai-master-apply')
            ?.addEventListener('click', () => { _applyMasterSelection(); });
    }
}

// GE.AIC.6 (2026-09-29): _initModelsSubTab (wired #ai-tool-tab-models +
// called initializeEmbeddingModelsPage()) removed along with the Models tab
// itself. Uploaded embedding models still work — embedding-models.js and its
// backing endpoints are untouched — but there is no longer an in-AI-Config
// upload/browse surface for them.

let _schedulerSubTabWired = false;

function _initSchedulerSubTab() {
    // Show the Scheduler sub-tab — main-tab-ai is already platform-admin
    // gated upstream, so non-admins never reach this code path. The
    // sub-tab button starts hidden in the HTML so the parent tab can be
    // visible for admins of other tools (Wizard / Adapter / Assistant)
    // even before this code runs; here we unhide it.
    const tabBtn = document.getElementById('ai-tool-tab-scheduler');
    if (tabBtn) tabBtn.classList.remove('hidden');

    if (_schedulerSubTabWired) {
        // Even if wiring is done, refresh the values so re-entering the
        // sub-tab shows the latest persisted state.
        _refreshSchedulerSubTab().catch(err => console.error('scheduler refresh failed:', err));
        return;
    }
    _schedulerSubTabWired = true;

    const presetSel = document.getElementById('scheduler-preset');
    const capInput = document.getElementById('scheduler-capacity-floor');
    const packerInput = document.getElementById('scheduler-packer-full');
    const varInput = document.getElementById('scheduler-variance-penalty');
    const bgInput = document.getElementById('scheduler-background-max');
    const capValue = document.getElementById('scheduler-capacity-floor-value');
    const packerValue = document.getElementById('scheduler-packer-full-value');
    const varValue = document.getElementById('scheduler-variance-penalty-value');
    const bgValue = document.getElementById('scheduler-background-max-value');
    const saveBtn = document.getElementById('scheduler-save-btn');
    const saveStatus = document.getElementById('scheduler-save-status');
    const sourceHint = document.getElementById('scheduler-source-hint');
    const trainingBtn = document.getElementById('scheduler-generate-training-btn');

    function _readSliders() {
        return {
            capacity_floor_pct: parseInt(capInput.value, 10),
            packer_full_above_util_pct: parseInt(packerInput.value, 10),
            variance_penalty_s: parseInt(varInput.value, 10),
            background_max_fleet_pct: parseInt(bgInput.value, 10),
        };
    }

    function _writeSliders(values) {
        capInput.value = values.capacity_floor_pct;
        packerInput.value = values.packer_full_above_util_pct;
        varInput.value = values.variance_penalty_s;
        bgInput.value = values.background_max_fleet_pct;
        capValue.textContent = values.capacity_floor_pct;
        packerValue.textContent = values.packer_full_above_util_pct;
        varValue.textContent = values.variance_penalty_s;
        bgValue.textContent = values.background_max_fleet_pct;
    }

    function _selectPresetInDropdown(name) {
        // The Custom option is `disabled hidden` — temporarily un-hide it
        // when we need to display Custom, otherwise hide it again.
        const customOpt = presetSel.querySelector('option[value="custom"]');
        if (name === 'custom') {
            customOpt.hidden = false;
            customOpt.disabled = false;
            presetSel.value = 'custom';
            customOpt.disabled = true;  // user can't pick it directly
        } else {
            presetSel.value = name;
            customOpt.hidden = true;
            customOpt.disabled = true;
        }
    }

    function _syncPresetFromSliders() {
        const vals = _readSliders();
        const detected = detectScheduleRuntimePreset(vals);
        _selectPresetInDropdown(detected);
    }

    presetSel.addEventListener('change', () => {
        const presetVals = getSchedulerPresetValues(presetSel.value);
        if (!presetVals) return;
        _writeSliders(presetVals);
        // No auto-save — Save button is the commit.
    });

    [capInput, packerInput, varInput, bgInput].forEach(input => {
        input.addEventListener('input', () => {
            capValue.textContent = capInput.value;
            packerValue.textContent = packerInput.value;
            varValue.textContent = varInput.value;
            bgValue.textContent = bgInput.value;
            _syncPresetFromSliders();
        });
    });

    saveBtn.addEventListener('click', async () => {
        saveStatus.classList.remove('hidden', 'text-red-600', 'text-green-600');
        saveStatus.classList.add('text-gray-500');
        saveStatus.textContent = 'Saving…';
        const vals = _readSliders();
        try {
            const persisted = await saveSchedulerRuntimeConfig({
                ...vals,
                preset_name: presetSel.value,
            });
            _writeSliders(persisted);
            _selectPresetInDropdown(persisted.preset_name);
            sourceHint.classList.add('hidden');
            saveStatus.classList.remove('text-gray-500', 'text-red-600');
            saveStatus.classList.add('text-green-600');
            saveStatus.textContent = `Saved (preset: ${persisted.preset_name})`;
            setTimeout(() => saveStatus.classList.add('hidden'), 3000);
        } catch (err) {
            saveStatus.classList.remove('text-gray-500', 'text-green-600');
            saveStatus.classList.add('text-red-600');
            saveStatus.textContent = `Save failed: ${err.message}`;
        }
    });

    trainingBtn.addEventListener('click', async () => {
        // Cancel any prior poll cycle so a fresh dispatch gets a clean bar.
        if (_schedulerTrainingPollHandle) {
            _schedulerTrainingPollHandle.stop();
            _schedulerTrainingPollHandle = null;
        }
        try {
            const result = await generateTrainingDataAction();
            if (!result) return;  // user cancelled the prompt
            if (result.message) {
                saveStatus.classList.remove('hidden', 'text-red-600', 'text-gray-500');
                saveStatus.classList.add('text-green-600');
                saveStatus.textContent = result.message;
                setTimeout(() => saveStatus.classList.add('hidden'), 5000);
            }
            _startSchedulerTrainingProgressBar(result);
        } catch (err) {
            saveStatus.classList.remove('hidden', 'text-green-600', 'text-gray-500');
            saveStatus.classList.add('text-red-600');
            saveStatus.textContent = `Training data: ${err.message}`;
        }
    });

    document.getElementById('scheduler-training-progress-hide')?.addEventListener('click', () => {
        if (_schedulerTrainingPollHandle) {
            _schedulerTrainingPollHandle.stop();
            _schedulerTrainingPollHandle = null;
        }
        document.getElementById('scheduler-training-progress')?.classList.add('hidden');
    });

    _refreshSchedulerSubTab().catch(err => console.error('scheduler initial load failed:', err));

    // Restore an in-flight training run if the user refreshed the page
    // mid-dispatch. sessionStorage holds the baseline + dispatched
    // counts; the polling helper recomputes the delta from current
    // /platform/training/status so the bar continues from wherever
    // workers actually are, not where it was when the page closed.
    _restoreSchedulerTrainingProgressIfActive();
}

let _schedulerTrainingPollHandle = null;

const _SCHED_TRAINING_STORAGE_KEY = 'mod_scheduler_training_run_v1';
// Drop snapshots older than this on restore — protects against stale
// state from runs that completed (or crashed) while the tab was closed.
const _SCHED_TRAINING_STALE_AFTER_MS = 24 * 60 * 60 * 1000;

function _saveTrainingRunSnapshot(dispatchResult) {
    try {
        sessionStorage.setItem(_SCHED_TRAINING_STORAGE_KEY, JSON.stringify({
            baseline: dispatchResult.baseline,
            dispatched: dispatchResult.dispatched,
            workers: dispatchResult.workers,
            estimated_hours: dispatchResult.estimated_hours,
            started_at: dispatchResult.started_at || Date.now(),
            saved_at: Date.now(),
        }));
    } catch (e) {
        console.warn('scheduler training snapshot save failed:', e);
    }
}

function _loadTrainingRunSnapshot() {
    try {
        const raw = sessionStorage.getItem(_SCHED_TRAINING_STORAGE_KEY);
        if (!raw) return null;
        const snap = JSON.parse(raw);
        if (Date.now() - (snap.saved_at || 0) > _SCHED_TRAINING_STALE_AFTER_MS) {
            sessionStorage.removeItem(_SCHED_TRAINING_STORAGE_KEY);
            return null;
        }
        return snap;
    } catch {
        return null;
    }
}

function _clearTrainingRunSnapshot() {
    try { sessionStorage.removeItem(_SCHED_TRAINING_STORAGE_KEY); } catch {}
}

function _formatSchedulerEta(seconds) {
    if (seconds == null || !isFinite(seconds) || seconds <= 0) return null;
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return `~${h}h ${m}m remaining`;
    if (m > 0) return `~${m}m remaining`;
    return `~${seconds}s remaining`;
}

function _formatElapsed(seconds) {
    if (seconds == null || seconds <= 0) return null;
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return `${h}h ${m}m elapsed`;
    if (m > 0) return `${m}m ${s}s elapsed`;
    return `${s}s elapsed`;
}

function _startSchedulerTrainingProgressBar(dispatchResult, { restoring = false } = {}) {
    const wrap = document.getElementById('scheduler-training-progress');
    const bar = document.getElementById('scheduler-training-progress-bar');
    const pctEl = document.getElementById('scheduler-training-progress-pct');
    const labelEl = document.getElementById('scheduler-training-progress-label');
    const detailEl = document.getElementById('scheduler-training-progress-detail');
    if (!wrap || !bar || !pctEl || !labelEl || !detailEl) return;

    const dispatched = dispatchResult.dispatched || 0;

    // Render the bar immediately with the target counts so the user
    // sees concrete numbers from frame 0 instead of waiting for the
    // first poll. The first onTick will overlay live data within ~2s.
    wrap.classList.remove('hidden');
    bar.style.width = '0%';
    pctEl.textContent = '0%';
    if (restoring) {
        labelEl.textContent = `Resuming run · 0 / ${dispatched.toLocaleString()} workflow runs completed`;
    } else {
        labelEl.textContent = `0 / ${dispatched.toLocaleString()} workflow runs queued across ${dispatchResult.workers || 0} workers`;
    }
    detailEl.textContent = dispatchResult.estimated_hours
        ? `Estimated total: ~${dispatchResult.estimated_hours}h · checking in 2s…`
        : 'checking in 2s…';

    const baseline = {
        ...dispatchResult.baseline,
        started_at_ms: dispatchResult.started_at || Date.now(),
    };

    if (dispatched <= 0) {
        labelEl.textContent = 'No workflow runs dispatched.';
        _clearTrainingRunSnapshot();
        return;
    }

    if (!restoring) _saveTrainingRunSnapshot(dispatchResult);

    _schedulerTrainingPollHandle = pollTrainingProgress({
        baseline,
        dispatched,
        intervalMsVisible: 2000,
        intervalMsHidden: 10000,
        rollingWindowSamples: 30,
        onTick({ completedDelta, pct: pctValue, etaSeconds, elapsedSeconds, status }) {
            bar.style.width = `${pctValue}%`;
            pctEl.textContent = `${pctValue.toFixed(0)}%`;
            labelEl.textContent = `${completedDelta.toLocaleString()} / ${dispatched.toLocaleString()} workflow runs completed`;
            const eta = _formatSchedulerEta(etaSeconds);
            const elapsed = _formatElapsed(elapsedSeconds);
            const samples = (status?.resource_samples ?? 0).toLocaleString();
            const parts = [];
            if (eta) parts.push(eta);
            if (elapsed) parts.push(elapsed);
            parts.push(`${samples} resource samples collected`);
            detailEl.textContent = parts.join(' · ');
        },
        onDone(status) {
            bar.style.width = '100%';
            pctEl.textContent = '100%';
            labelEl.textContent = 'Training data generation complete';
            detailEl.textContent = `${(status?.resource_samples ?? 0).toLocaleString()} total resource samples`;
            _schedulerTrainingPollHandle = null;
            _clearTrainingRunSnapshot();
        },
        onError(err) {
            // Transient — surface but keep polling.
            console.warn('training progress poll error:', err);
            detailEl.textContent = `Polling error (will retry): ${err.message}`;
        },
    });
}

function _restoreSchedulerTrainingProgressIfActive() {
    const snap = _loadTrainingRunSnapshot();
    if (!snap) return;
    _startSchedulerTrainingProgressBar({
        baseline: snap.baseline,
        dispatched: snap.dispatched,
        workers: snap.workers,
        estimated_hours: snap.estimated_hours,
        started_at: snap.started_at,
    }, { restoring: true });
}

async function _refreshSchedulerSubTab() {
    const presetSel = document.getElementById('scheduler-preset');
    const sourceHint = document.getElementById('scheduler-source-hint');
    if (!presetSel || !sourceHint) return;
    const snap = await loadSchedulerRuntimeConfig();
    document.getElementById('scheduler-capacity-floor').value = snap.capacity_floor_pct;
    document.getElementById('scheduler-packer-full').value = snap.packer_full_above_util_pct;
    document.getElementById('scheduler-variance-penalty').value = snap.variance_penalty_s;
    document.getElementById('scheduler-background-max').value = snap.background_max_fleet_pct;
    document.getElementById('scheduler-capacity-floor-value').textContent = snap.capacity_floor_pct;
    document.getElementById('scheduler-packer-full-value').textContent = snap.packer_full_above_util_pct;
    document.getElementById('scheduler-variance-penalty-value').textContent = snap.variance_penalty_s;
    document.getElementById('scheduler-background-max-value').textContent = snap.background_max_fleet_pct;

    const customOpt = presetSel.querySelector('option[value="custom"]');
    if (snap.preset_name === 'custom') {
        customOpt.hidden = false;
        customOpt.disabled = false;
        presetSel.value = 'custom';
        customOpt.disabled = true;
    } else {
        presetSel.value = snap.preset_name;
        customOpt.hidden = true;
        customOpt.disabled = true;
    }

    if (snap.source !== 'db') {
        sourceHint.classList.remove('hidden');
    } else {
        sourceHint.classList.add('hidden');
    }
}

// Build the PUT body for one tool from its current form DOM. All tool
// forms are rendered (just hidden when not the active tab), so this
// reads live values for ANY tab — which is what lets "Save all" persist
// every tab at once without the user visiting each.
function _buildToolBody(tool) {
    _captureFormState(tool);
    const form = document.querySelector(`#ai-tool-${tool} form`);
    if (!form) return null;
    _captureFallbackState(tool, form);
    const state = _aiFormState[tool];

    // ai-configuration-page redesign — the tool tabs present one full "Managed
    // model" block (Provider covers ollama / OpenAI-compatible / cloud, so there
    // is no Execution Mode dropdown). That maps to the backend's `advanced`
    // execution mode, which persists every field below verbatim. Managed
    // (system) mode is set separately by the master bar's Apply.
    const body = { execution_mode: 'advanced' };

    // GE.AIC.1-3 — stack_id/served_model only mean anything when
    // provider === "stack"; only sent in that case so an unrelated
    // provider switch never carries a stale stack pin along with it.
    const chatFields = ['provider', 'model', 'base_url', 'compute_target', 'keep_alive', 'temperature', 'max_tokens'];
    if (state.provider === 'stack') chatFields.push('stack_id', 'served_model');
    chatFields.forEach((k) => {
        if (state[k] !== undefined && state[k] !== '') body[k] = state[k];
    });
    if (state.api_key && typeof state.api_key === 'string' && state.api_key !== '') {
        body.api_key = state.api_key;
    }

    // Node Wizard code-review (Review reasoner) setting — wizard-only.
    if (tool === 'wizard' && state.critic_mode) body.critic_mode = state.critic_mode;

    // Trimmed embedding-model pins (orthogonal to the LLM block).
    //  Node Wizard → single embedding_model (learning store).
    //  AI Assistant → a store sub-choice + a pin per store (User RAG / MOD docs).
    // GE.AIC.1-3 — each embedding slot also carries a provider ("platform" |
    // "stack") + stack_id; embedding_model doubles as the served_model when
    // provider is "stack" (kept minimal — no separate served-model field).
    if (tool === 'wizard' && state.embedding_model !== undefined) {
        body.embedding_model = state.embedding_model;
        if (state.embedding_provider) body.embedding_provider = state.embedding_provider;
        if (state.embedding_provider === 'stack' && state.embedding_stack_id) {
            body.embedding_stack_id = state.embedding_stack_id;
        }
    } else if (tool === 'assistant') {
        if (state.embedding_store !== undefined) body.embedding_store = state.embedding_store;
        if (state.embedding_model_user_rag !== undefined) body.embedding_model_user_rag = state.embedding_model_user_rag;
        if (state.embedding_model_docs !== undefined) body.embedding_model_docs = state.embedding_model_docs;
        if (state.embedding_provider_user_rag) body.embedding_provider_user_rag = state.embedding_provider_user_rag;
        if (state.embedding_provider_user_rag === 'stack' && state.embedding_stack_id_user_rag) {
            body.embedding_stack_id_user_rag = state.embedding_stack_id_user_rag;
        }
        if (state.embedding_provider_docs) body.embedding_provider_docs = state.embedding_provider_docs;
        if (state.embedding_provider_docs === 'stack' && state.embedding_stack_id_docs) {
            body.embedding_stack_id_docs = state.embedding_stack_id_docs;
        }
    }

    // GE.MNL.4b — opt-in per-tool LLM fallback. Always send both: an empty
    // fallback_tool clears the stored target (matches the PUT's own
    // fallback_tool="" clear semantics), which keeps unchecking the box and
    // saving an actual "off" rather than a checkbox the next load silently
    // re-enables from a target left behind in Vault.
    body.fallback_enabled = !!state.fallback_enabled;
    body.fallback_tool = state.fallback_enabled ? (state.fallback_tool || '') : '';

    return body;
}

// Save EVERY AI tool's current settings in one click. The per-tab forms
// each submit here, so the admin can set all tabs, then hit Save once.
async function handleSaveAllAIConfig(e) {
    if (e) e.preventDefault();
    const token = sessionStorage.getItem('accessToken');
    if (!token) return;
    const status = document.getElementById('ai-save-status');
    // rc20-aicfg-gate (CE-ship 11-08) — ai_config.py SHIPS in CE: open-core/manifest.yaml:812
    // lists CORE/API/routers/ai_config.py under api_allow (AA-CE comment :811) and CORE/API/main.py:2343
    // mounts the router, so PUT /platform/ai-config/{tool} (routers/ai_config.py:3757) answers on CE and
    // the User-RAG / Node Wizard / Repair embedding picks must persist there (owner 11-08: keep VISIBLE
    // AND WORKING in CE, tasks.md:330 BUG.B58). The only paid-only write left on this path is the critic
    // pseudo-tool below, which keeps its own edition guard.
    const errors = [];
    for (const tool of AI_TOOLS) {
        const body = _buildToolBody(tool);
        if (!body) continue;
        try {
            const resp = await fetch(`${window.APP_CONFIG?.API_URL || ''}/platform/ai-config/${tool}`, {
                method: 'PUT',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                errors.push(`${tool}: ${err.detail || resp.statusText}`);
            }
        } catch (err) {
            errors.push(`${tool}: ${err.message}`);
        }
    }

    // separate-reasoner-model — persist the separate reviewer/reasoner (critic
    // pseudo-tool) ONLY when the Node Wizard's code review is set to "Separate
    // reasoner model". In "self"/"off" we leave the stored critic config
    // untouched, so a configured reasoner survives toggling review off and on.
    const _wizForm = document.querySelector('#ai-tool-wizard form');
    if (_wizForm) {
        _captureCriticState(_wizForm);
        const criticMode = (_aiFormState.wizard || {}).critic_mode
            || (_wizForm.querySelector('[data-field="critic_mode"]') || {}).value;
        if (criticMode === 'reasoner' && await isPaidEdition()) {
            const criticBody = _buildCriticBody();
            try {
                const resp = await fetch(`${window.APP_CONFIG?.API_URL || ''}/platform/ai-config/critic`, {
                    method: 'PUT',
                    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                    body: JSON.stringify(criticBody),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    errors.push(`critic: ${err.detail || resp.statusText}`);
                }
            } catch (err) {
                errors.push(`critic: ${err.message}`);
            }
        }
    }

    if (errors.length) {
        showError('Some AI settings failed to save — ' + errors.join('; '));
    } else {
        if (status) {
            status.textContent = `Saved all AI tool settings (${AI_TOOLS.join(', ')}).`;
            status.classList.remove('hidden');
            setTimeout(() => status.classList.add('hidden'), 4000);
        }
        // Per-tab inline confirmation next to each Save button.
        document.querySelectorAll('.ai-tool-form [data-role="save-status"]').forEach((el) => {
            el.textContent = 'Saved.';
            el.classList.remove('hidden');
            setTimeout(() => el.classList.add('hidden'), 4000);
        });
    }
}

// Kept for back-compat with any cached page still wiring per-tool saves;
// now just saves everything (the user expects one Save to persist all).
async function handleSaveToolAIConfig(e, _tool) {
    return handleSaveAllAIConfig(e);
}

// Back-compat: the older form-level handler is gone; keep a no-op stub so
// stale event-listener hookups don't crash older cached pages.
async function handleSaveAIConfig() { /* deprecated; per-tool handler is used instead */ }
function updateAIProviderHints() { /* deprecated */ }


// =========================================================================
// Integrations Tab
// =========================================================================

// Provider presets (host / port / encryption / hint) live in ./smtp-presets.js.
// They are UI-only: the API always receives provider='custom' (or 'postmark')
// plus host/port/security, so the backend provider enum is unchanged.

function _updateSmtpProviderHint() {
    const sel = document.getElementById('int-smtp-provider');
    const hint = document.getElementById('int-smtp-provider-hint');
    if (!sel || !hint) return;
    const preset = SMTP_PRESETS[sel.value];
    if (!preset) return;
    // Static developer text (no server strings), so innerHTML is safe here.
    hint.innerHTML = preset.hint;
    hint.className = preset.oauthOnly ? 'text-xs text-red-600 mt-1' : 'text-xs text-gray-500 mt-1';
}

// Show the right credential UI for the selected provider:
//   postmark → single "Server Token" field (we send it as both SMTP user + pass)
//   custom   → traditional Username + Password fields
function _updateSmtpCredentialBlocks() {
    const sel = document.getElementById('int-smtp-provider');
    if (!sel) return;
    const postmarkBlock = document.getElementById('int-smtp-postmark-block');
    const credsBlock = document.getElementById('int-smtp-creds-block');
    if (!postmarkBlock || !credsBlock) return;
    if (sel.value === 'postmark') {
        postmarkBlock.classList.remove('hidden');
        credsBlock.classList.add('hidden');
    } else {
        postmarkBlock.classList.add('hidden');
        credsBlock.classList.remove('hidden');
    }
}

function _onSmtpProviderChange() {
    const sel = document.getElementById('int-smtp-provider');
    if (!sel) return;
    const f = smtpPresetFields(sel.value);
    if (!f) return;
    // 'custom' keeps whatever the operator typed; OAuth-only entries fill nothing.
    if (f.fill) {
        const hostEl = document.getElementById('int-smtp-host');
        const portEl = document.getElementById('int-smtp-port');
        const secEl = document.getElementById('int-smtp-security');
        if (hostEl) hostEl.value = f.host;
        if (portEl) portEl.value = f.port;
        if (secEl) secEl.value = f.security;
    }
    const userEl = document.getElementById('int-smtp-user');
    if (userEl) userEl.placeholder = f.userPlaceholder;
    _updateSmtpProviderHint();
    _updateSmtpCredentialBlocks();
}

function _wireIntegrationsSubTabs() {
    // Guard PER-ELEMENT (not a module flag): the Dashboard re-fetches
    // settings.html on every navigation, so the buttons are fresh DOM each
    // visit and must be re-bound. A module-level flag would leave the new
    // buttons dead after the first visit.
    document.querySelectorAll('.int-tab').forEach((btn) => {
        if (btn.dataset.intWired === '1') return;
        btn.dataset.intWired = '1';
        btn.addEventListener('click', () => {
            const which = btn.dataset.int;
            document.querySelectorAll('.int-tab').forEach((b) => {
                const active = b.dataset.int === which;
                b.classList.toggle('border-primary', active);
                b.classList.toggle('text-primary', active);
                b.classList.toggle('border-transparent', !active);
                b.classList.toggle('text-gray-500', !active);
            });
            // BUG.B36c — only Email + Mobile sub-tabs exist now (the webhooks
            // sub-tab is retired; webhooks live on the Apps tab).
            const emailP = document.getElementById('int-subpanel-email');
            const mobileP = document.getElementById('int-subpanel-mobile');
            if (emailP) emailP.classList.toggle('hidden', which !== 'email');
            if (mobileP) mobileP.classList.toggle('hidden', which !== 'mobile');
        });
    });
}

async function loadIntegrations() {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    const headers = { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' };

    // Mount guard: everything below hydrates the Integrations panel (int-* /
    // me-* / wh-* ids). ui.js re-injects page HTML on every navigation, so a
    // page-swap to another page (analytics tabs, stack-tls, pricing-config…)
    // leaves those ids out of the DOM; the unguarded `.value =` writes would
    // then throw "Cannot set properties of null (setting 'value')" and the
    // e2e console guard would fail the run. No-op when the panel is mounted.
    if (!document.getElementById('main-panel-integrations')) return;

    // Integrations is segmented into sub-tabs (Email / Mobile app). Wire the
    // switcher once, then the mobile enrollment section (me-* IDs).
    _wireIntegrationsSubTabs();

    // 2400LR-pg6a: /mobile/*, /mobile-devices and mobile-signing are denied on CE (manifest
    // :1562); hide the whole Mobile app sub-tab and skip its inits (fail closed: unknown or
    // errored = CE = hidden; only a clean saas/pro edition shows it).
    const _ceNoMobile = await getPlatformFeatures().then((f) => {
        const ent = f && f.entitlements;
        return !(ent && !ent.error && (ent.edition === 'saas' || ent.edition === 'pro'));
    }).catch(() => true);
    if (_ceNoMobile) {
        document.getElementById('int-tab-mobile')?.classList.add('hidden');
        document.getElementById('int-subpanel-mobile')?.classList.add('hidden');
    }

    // The "Outbound Webhooks moved to System → Apps → Webhooks" signpost points at
    // the Apps tab, which is hidden on CE (analytics.js _ceEditionAn) and has no CE
    // webhooks screen (post-launch CUT-B6). Remove the dead link there; same
    // fail-closed isPaidEdition() gate as the Apps tab itself (unknown = CE = hide).
    if (!await isPaidEdition()) {
        document.getElementById('int-webhooks-signpost')?.classList.add('hidden');
    }

    // Mobile-app enrollment section (me-* IDs) lives in the Mobile sub-tab now.
    // Idempotent — the module guards its own listeners.
    if (!_ceNoMobile) try { initializeMobileEnrollmentPage(); } catch (e) { console.error('mobile enrollment init failed:', e); }

    // CUIP.14b — enrolled-phones panel: same Mobile sub-tab, container built by
    // the module itself (no new markup, no sidebar row).
    if (!_ceNoMobile) try { initializeMobileDevicesPanel(); } catch (e) { console.error('mobile devices init failed:', e); }

    // sz-4.9b — OAuth connections panel (int-oauth-connections). Idempotent —
    // the module rebuilds its own DOM from scratch on each call.
    // 2400LR-pg5: oauth_connections.py is denied on CE (manifest :1257); skip the panel
    // there (fail closed: unknown or errored = CE = hidden). Budgets already self-hides on the 404 gate.
    getPlatformFeatures().then((f) => {
        const ent = f && f.entitlements;
        return !(ent && !ent.error && (ent.edition === 'saas' || ent.edition === 'pro'));
    }).catch(() => true).then((isCe) => {
        if (isCe) { document.getElementById('int-oauth-connections')?.classList.add('hidden'); return; }
        try { initializeSettingsOauthConnections(); } catch (e) { console.error('oauth connections init failed:', e); }
    });

    // GT.D8 — Kubernetes cluster registry card (js/k8s-clusters.js). The k8s_clusters
    // router IS CE-allowed (open-core/manifest.yaml), so no edition gate here; the
    // module self-hides on a 404/403 from the API. Idempotent — it rebuilds its own
    // DOM from scratch on each call.
    try { initializeK8sClustersPage(); } catch (e) { console.error('k8s clusters init failed:', e); }

    // sz-4.x — Workflow budgets panel (int-budgets). Idempotent — the module
    // rebuilds its own DOM from scratch on each call. initBudgetsPanel is async:
    // attach .catch() to the returned promise so a rejection is logged, not
    // left unhandled.
    // FIX-CE-HIDE-BUDGETS-TAB (owner decision owner 11-11 ~2:00 PM CT, answer B): credit
    // spend caps are a QUANTITY CAP, which CE must not have — hide Settings > Budgets on
    // CE. Positive-PAID gate (same predicate as the OAuth panel above): ONLY a clean
    // saas/pro edition shows the panel; CE, an unknown edition, an entitlements error and
    // a rejected getPlatformFeatures() call all HIDE it and skip initBudgetsPanel, so no
    // fetch is made to the CE-denied /budgets router (workflow_budgets.py, manifest:1284).
    // The old _ceNoMobile predicate (edition === 'ce' && !error) failed OPEN on unknown/
    // errored editions; this fails closed. The panel's "Workflow budgets" heading is built
    // by initBudgetsPanel itself, so skipping the init leaves no empty section behind.
    getPlatformFeatures().then((f) => {
        const ent = f && f.entitlements;
        return !(ent && !ent.error && (ent.edition === 'saas' || ent.edition === 'pro'));
    }).catch(() => true).then((isCe) => {
        if (isCe) { document.getElementById('int-budgets')?.classList.add('hidden'); return; }
        try { initBudgetsPanel(document.getElementById('int-budgets')).catch((e) => console.error('budgets panel init failed:', e)); } catch (e) { console.error('budgets panel init failed:', e); }
    });

    // BUG.B36c — the Outbound Webhooks sub-tab is RETIRED here: settings.js no
    // longer calls the webhooks initializer and has no tenant-root gate on a
    // webhooks tab button. B36a/B36b consolidated every webhook into the ONE
    // "Webhooks" section on the Apps tab (System → Apps → Webhooks); pages/settings.html
    // carries a signpost link (#analytics?tab=apps) and ui.js redirects the old
    // #webhooks bookmark there. js/webhooks.js itself stays (js/apps.js imports
    // its endpoint helpers).
    // consolidated every webhook into the ONE "Webhooks" section on the Apps tab
    // (System → Apps → Webhooks); pages/settings.html carries a signpost link
    // (#analytics?tab=apps) and ui.js redirects the old #webhooks bookmark there.
    // js/webhooks.js itself stays (js/apps.js imports its endpoint helpers).

    // Everyone can reach the Mobile app sub-tab (install + enroll their own
    // login). SMTP configuration is platform-admin only — hide that sub-tab for
    // regular users, land them on Mobile, and skip the admin-only SMTP load.
    const isAdmin = !!(currentUser && currentUser.is_platform_admin);
    if (!isAdmin) {
        const emailTab = document.getElementById('int-tab-email');
        if (emailTab) emailTab.classList.add('hidden');
        const mobileTab = document.getElementById('int-tab-mobile');
        // 2400LR-pg6aR: on CE the Mobile sub-tab is hidden above; clicking it would
        // run the switcher and UN-hide int-subpanel-mobile. Skip the click there.
        if (mobileTab && !_ceNoMobile) mobileTab.click();  // reuse the switcher → show Mobile panel
        return;
    }

    // Load platform email config (Vault-backed, /admin/email-config — see launch-day-followup-5 §1A).
    try {
        const resp = await fetch(`${apiUrl}/system/email`, { headers });
        if (resp.ok) {
            const cfg = await resp.json();
            // Null-safe per element (same idiom as _onSmtpProviderChange): the
            // panel can be mid-swap while this async fetch is in flight.
            const hostEl = document.getElementById('int-smtp-host');
            const portEl = document.getElementById('int-smtp-port');
            const fromEmailEl = document.getElementById('int-smtp-from-email');
            const fromNameEl = document.getElementById('int-smtp-from-name');
            if (hostEl) hostEl.value = cfg.smtp_host || '';
            if (portEl) portEl.value = cfg.smtp_port || 587;
            if (fromEmailEl) fromEmailEl.value = cfg.sender_email || '';
            if (fromNameEl) fromNameEl.value = cfg.sender_name || '';
            // Provider preset selector — infer from saved host if the
            // backend doesn't set provider explicitly. Postmark is the
            // only host the dropdown auto-detects right now; everything
            // else falls back to "custom" SMTP.
            // Presets are UI-only (saved provider is 'custom'), so the preset is
            // inferred from the saved host; unknown hosts show as Custom.
            const providerSel = document.getElementById('int-smtp-provider');
            const presetKey = inferSmtpPreset(cfg.smtp_host, cfg.provider);
            const isPostmark = presetKey === 'postmark';
            if (providerSel) {
                providerSel.value = presetKey;
                _updateSmtpProviderHint();
                _updateSmtpCredentialBlocks();
            }
            const secEl = document.getElementById('int-smtp-security');
            if (secEl) secEl.value = cfg.smtp_security || 'auto';
            const selfSignedEl = document.getElementById('int-smtp-selfsigned');
            if (selfSignedEl) selfSignedEl.checked = cfg.smtp_allow_selfsigned === true;
            // Populate the credential fields for the active provider.
            // Postmark: only the single Server Token field; the saved
            // smtp_user is also the token by convention so we show it
            // there for context (still masked as it's password-typed).
            if (isPostmark) {
                const tokenEl = document.getElementById('int-smtp-server-token');
                if (tokenEl) {
                    tokenEl.value = cfg.smtp_user || '';
                    if (cfg.password_set) tokenEl.placeholder = '(configured — leave blank to keep)';
                }
            } else {
                const userEl = document.getElementById('int-smtp-user');
                const passEl = document.getElementById('int-smtp-pass');
                if (userEl) userEl.value = cfg.smtp_user || '';
                if (cfg.password_set && passEl) {
                    passEl.placeholder = '(configured — leave blank to keep)';
                }
            }
            const lastTestedEl = document.getElementById('int-smtp-last-tested');
            if (lastTestedEl) {
                if (cfg.last_tested_at) {
                    const when = new Date(cfg.last_tested_at).toLocaleString();
                    if (cfg.last_test_result === 'success') {
                        lastTestedEl.textContent = `Last tested ${when} — succeeded`;
                        lastTestedEl.className = 'text-xs text-green-600';
                    } else {
                        const err = cfg.last_test_error ? ` (${cfg.last_test_error})` : '';
                        lastTestedEl.textContent = `Last tested ${when} — failed${err}`;
                        lastTestedEl.className = 'text-xs text-red-600';
                    }
                } else {
                    lastTestedEl.textContent = 'Not tested yet';
                    lastTestedEl.className = 'text-xs text-gray-500';
                }
            }
        }
    } catch (e) {
        console.error('Failed to load email config:', e);
    }

    // Load email verification setting
    try {
        const resp = await fetch(`${apiUrl}/integrations/email-verification`, { headers });
        if (resp.ok) {
            const data = await resp.json();
            const toggle = document.getElementById('email-verification-toggle');
            if (toggle) {
                toggle.checked = data.email_verification_required;
                toggle.addEventListener('change', async () => {
                    const statusEl = document.getElementById('email-verification-status');
                    try {
                        const r = await fetch(`${apiUrl}/integrations/email-verification?enabled=${toggle.checked}`, {
                            method: 'POST', headers,
                        });
                        if (r.ok) {
                            statusEl.textContent = toggle.checked ? 'Email verification enabled' : 'Email verification disabled';
                            statusEl.className = 'text-xs text-green-600 mt-1 block';
                        } else {
                            toggle.checked = !toggle.checked;
                            statusEl.textContent = 'Failed to update';
                            statusEl.className = 'text-xs text-red-600 mt-1 block';
                        }
                    } catch (e) {
                        toggle.checked = !toggle.checked;
                        statusEl.textContent = 'Failed to update';
                        statusEl.className = 'text-xs text-red-600 mt-1 block';
                    }
                });
            }
        }
    } catch (e) {
        console.error('Failed to load email verification setting:', e);
    }
}

async function handleSaveSmtp(e) {
    e.preventDefault();

    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    const status = document.getElementById('int-smtp-status');

    const providerSel = document.getElementById('int-smtp-provider');
    const presetKey = providerSel ? providerSel.value : 'custom';
    // Presets are UI sugar: only Postmark keeps its own provider value.
    const provider = smtpProviderForSave(presetKey);
    const securityEl = document.getElementById('int-smtp-security');
    // Postmark stays exactly as before: port-based auto, full certificate checks.
    const security = (provider === 'postmark' || !securityEl) ? 'auto' : securityEl.value;
    const selfSignedEl = provider === 'postmark' ? null : document.getElementById('int-smtp-selfsigned');

    // Resolve the credential pair from whichever block is active.
    // Postmark: one Server Token field is sent as both smtp_user AND
    // smtp_password (Postmark requires identical credentials in both).
    // Custom:   traditional separate Username + Password.
    let smtpUser, secretField, secretValue, isPreserving;
    if (provider === 'postmark') {
        const tokenField = document.getElementById('int-smtp-server-token');
        secretField = tokenField;
        secretValue = tokenField.value.trim();
        isPreserving = tokenField.placeholder.includes('configured') && secretValue === '';
        smtpUser = secretValue;  // username = the token
    } else {
        smtpUser = document.getElementById('int-smtp-user').value.trim();
        secretField = document.getElementById('int-smtp-pass');
        secretValue = secretField.value;
        isPreserving = secretField.placeholder.includes('configured') && secretValue === '';
    }

    // An unauthenticated server (local relay, smtp-relay.gmail.com): no username
    // and an explicit Encryption choice. Auto keeps the old "password required" rule.
    const noAuth = provider !== 'postmark' && !smtpUser && security !== 'auto';

    if (!noAuth && !secretValue && !isPreserving) {
        showInfo(provider === 'postmark' ? 'Server Token is required' : 'Password is required');
        return;
    }

    // /admin/email-config three-way password semantics:
    //   smtp_password omitted  → preserve existing Vault value
    //   smtp_password === ""   → clear (unconfigure)
    //   non-empty              → write to Vault
    const body = {
        provider,
        smtp_host: document.getElementById('int-smtp-host').value.trim(),
        smtp_port: parseInt(document.getElementById('int-smtp-port').value) || 587,
        smtp_user: smtpUser,
        sender_email: document.getElementById('int-smtp-from-email').value.trim(),
        sender_name: document.getElementById('int-smtp-from-name').value.trim() || 'MOD Platform',
        smtp_security: security,
        smtp_allow_selfsigned: !!(selfSignedEl && selfSignedEl.checked),
    };
    if (noAuth) {
        body.smtp_password = '';  // clear any stored password: this server needs none
    } else if (!isPreserving) {
        body.smtp_password = secretValue;
    }

    try {
        const resp = await fetch(`${apiUrl}/system/email`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (resp.ok) {
            status.textContent = 'Email config saved';
            status.className = 'text-sm text-green-600';
            setTimeout(() => { status.textContent = ''; }, 3000);
            // Refresh the panel so password_set flag + last_tested status are accurate.
            if (secretField) secretField.value = '';
            await loadIntegrations();
        } else {
            const err = await resp.json().catch(() => ({}));
            status.textContent = 'Failed: ' + (err.detail || 'Unknown');
            status.className = 'text-sm text-red-600';
        }
    } catch (e) {
        status.textContent = 'Error: ' + e.message;
        status.className = 'text-sm text-red-600';
    }
}

async function handleTestSmtp() {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    const status = document.getElementById('int-smtp-status');

    // Default recipient to the logged-in user's email if empty.
    const recipientField = document.getElementById('int-smtp-test-recipient');
    let recipient = recipientField ? recipientField.value.trim() : '';
    if (!recipient) {
        try {
            const meResp = await fetch(`${apiUrl}/users/me`, {
                headers: { 'Authorization': `Bearer ${token}` },
            });
            if (meResp.ok) {
                const me = await meResp.json();
                recipient = me.email || '';
                if (recipientField && recipient) recipientField.value = recipient;
            }
        } catch (_) {
            /* fall through */
        }
    }
    if (!recipient) {
        status.textContent = 'Enter a recipient email';
        status.className = 'text-sm text-red-600';
        return;
    }

    status.textContent = 'Sending test…';
    status.className = 'text-sm text-primary';

    try {
        const resp = await fetch(`${apiUrl}/system/email/test`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ recipient_email: recipient }),
        });

        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.success) {
            status.textContent = `Sent to ${recipient}`;
            status.className = 'text-sm text-green-600';
        } else {
            status.textContent = data.error || data.detail || 'Test failed';
            status.className = 'text-sm text-red-600';
        }
        // Refresh last-tested indicator regardless of outcome.
        await loadIntegrations();
    } catch (e) {
        status.textContent = 'Error: ' + e.message;
        status.className = 'text-sm text-red-600';
    }
}


// =========================================================================
// Storage Tab — launch-day-followup-6 §1
// =========================================================================
//
// Read-only platform-admin panel showing where MinIO writes its data
// plus filesystem usage. Backed by GET /admin/storage/status. To change
// the path, the operator SSHes to the host and re-runs
// start-main-server.sh — there is no mutation endpoint on the API side.

function _formatBytes(bytes) {
    if (typeof bytes !== 'number' || !isFinite(bytes) || bytes < 0) return '—';
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const value = bytes / Math.pow(1024, i);
    const decimals = value >= 100 || i === 0 ? 0 : value >= 10 ? 1 : 2;
    return `${value.toFixed(decimals)} ${units[i]}`;
}

async function loadStorage() {
    // 2400LR-cdu: admin_storage.py denied on CE (manifest:1370); fail-open on unknown edition.
    if (!await isPaidEdition()) {
        document.getElementById('storage-loading')?.classList.add('hidden');
        document.getElementById('storage-card')?.classList.add('hidden');
        document.getElementById('storage-error')?.classList.add('hidden');
        return;
    }
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    const headers = { 'Authorization': `Bearer ${token}` };

    const loadingEl = document.getElementById('storage-loading');
    const errorEl = document.getElementById('storage-error');
    const cardEl = document.getElementById('storage-card');

    if (loadingEl) loadingEl.classList.remove('hidden');
    if (errorEl) errorEl.classList.add('hidden');
    if (cardEl) cardEl.classList.add('hidden');

    try {
        const resp = await fetch(`${apiUrl}/system/storage-status`, { headers });
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            const detail = body.detail || `HTTP ${resp.status}`;
            if (errorEl) {
                errorEl.textContent = `Failed to load storage status: ${detail}`;
                errorEl.classList.remove('hidden');
            }
            if (loadingEl) loadingEl.classList.add('hidden');
            return;
        }
        const data = await resp.json();

        // Storage health warnings (network-FS / low space). Most severe first.
        const warnEl = document.getElementById('storage-warnings');
        if (warnEl) {
            warnEl.innerHTML = '';
            const styles = {
                critical: 'bg-red-50 border-red-300 text-red-800',
                warning:  'bg-amber-50 border-amber-300 text-amber-800',
                info:     'bg-blue-50 border-blue-200 text-blue-800',
            };
            const icon = { critical: '⛔', warning: '⚠️', info: 'ℹ️' };
            (data.warnings || []).forEach(function (w) {
                const div = document.createElement('div');
                div.className = 'border rounded-md p-3 text-sm flex gap-2 ' + (styles[w.level] || styles.info);
                div.innerHTML = '<span>' + (icon[w.level] || 'ℹ️') + '</span><span>' +
                    String(w.message || '').replace(/[<>&]/g, function (c) {
                        return { '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c];
                    }) + '</span>';
                warnEl.appendChild(div);
            });
        }

        document.getElementById('storage-data-path').textContent = data.data_path || '—';
        document.getElementById('storage-fs-type').textContent = data.filesystem_type || 'unknown';
        document.getElementById('storage-total').textContent = _formatBytes(data.bytes_total);
        document.getElementById('storage-used').textContent = _formatBytes(data.bytes_used);
        document.getElementById('storage-free').textContent = _formatBytes(data.bytes_free);

        const pct = data.bytes_total > 0 ? (data.bytes_used / data.bytes_total) * 100 : 0;
        const bar = document.getElementById('storage-usage-bar');
        const pctEl = document.getElementById('storage-usage-pct');
        if (bar) bar.style.width = `${Math.min(100, Math.max(0, pct)).toFixed(1)}%`;
        if (pctEl) pctEl.textContent = `${pct.toFixed(1)}% of capacity used`;

        const refreshedEl = document.getElementById('storage-last-refreshed');
        if (refreshedEl && data.last_refreshed_at) {
            const when = new Date(data.last_refreshed_at).toLocaleString();
            refreshedEl.textContent = `Last refreshed ${when}`;
        }

        if (cardEl) cardEl.classList.remove('hidden');
        if (loadingEl) loadingEl.classList.add('hidden');
    } catch (e) {
        if (errorEl) {
            errorEl.textContent = `Failed to load storage status: ${e.message}`;
            errorEl.classList.remove('hidden');
        }
        if (loadingEl) loadingEl.classList.add('hidden');
    }
}


// ============================================================================
// δ §18 — Training sub-tab. Platform-admin only. Wraps the existing
// /calibration/plan endpoints (GET, POST, POST /{id}/cancel) with a
// simple on/off toggle + status display + target-in-flight slider.
// ============================================================================

let _trainingSubTabWired = false;
let _trainingPollHandle = null;

function _initTrainingSubTab() {
    const tabBtn = document.getElementById('ai-tool-tab-training');
    if (tabBtn) tabBtn.classList.remove('hidden');

    if (_trainingSubTabWired) {
        _refreshTrainingState().catch(err => console.error('training refresh failed:', err));
        _refreshWorkerConcurrency().catch(err => console.error('worker concurrency refresh failed:', err));
        return;
    }
    _trainingSubTabWired = true;

    const slider = document.getElementById('training-target-slider');
    const valueEl = document.getElementById('training-target-value');
    const toggle = document.getElementById('training-enable-toggle');

    if (slider && valueEl) {
        slider.addEventListener('input', () => {
            valueEl.textContent = slider.value;
        });
    }
    if (toggle) {
        toggle.addEventListener('change', _handleTrainingToggle);
    }

    // Clean & restart button — cancel running plan + start fresh in one
    // click. Visibility is driven by _renderTrainingState below (hidden
    // unless a plan is actively running).
    const restartBtn = document.getElementById('training-restart-btn');
    if (restartBtn) {
        restartBtn.addEventListener('click', _handleTrainingRestart);
    }

    // δ §11 worker-concurrency slider wiring.
    const wcSlider = document.getElementById('training-worker-concurrency-slider');
    const wcValueEl = document.getElementById('training-worker-concurrency-value');
    if (wcSlider && wcValueEl) {
        wcSlider.addEventListener('input', () => {
            wcValueEl.textContent = wcSlider.value === '0' ? 'unlimited' : wcSlider.value;
        });
        wcSlider.addEventListener('change', _handleWorkerConcurrencyChange);
    }

    _refreshTrainingState().catch(err => console.error('training initial load failed:', err));
    _refreshWorkerConcurrency().catch(err => console.error('worker concurrency initial load failed:', err));
}

async function _trainingApiUrl() {
    return window.APP_CONFIG?.API_URL || 'http://api.localhost';
}

function _trainingAuthHeader() {
    const token = sessionStorage.getItem('accessToken');
    return token ? { 'Authorization': `Bearer ${token}` } : {};
}

let _currentTrainingPlan = null;

async function _refreshTrainingFixtureBanner(apiUrl) {
    const banner = document.getElementById('training-fixture-banner');
    if (!banner) return;
    try {
        const resp = await fetch(`${apiUrl}/admin/fixtures`, {
            headers: _trainingAuthHeader(),
        });
        if (!resp.ok) { banner.classList.add('hidden'); return; }
        const statuses = await resp.json();
        const entry = (Array.isArray(statuses) ? statuses : []).find(
            (t) => t && Number(t.tenant_id) === Number(currentUser && currentUser.tenant_id)
        );
        // Lead review 2026-09-29: null = never migrated (pre-PFM.19 cutover) →
        // stay hidden, matching the flag-gated calibration gate.
        if (entry && entry.fixture_status && entry.fixture_status !== 'ready') {
            banner.classList.remove('hidden');
        } else {
            banner.classList.add('hidden');
        }
    } catch (_) {
        banner.classList.add('hidden');
    }
}

async function _refreshTrainingState() {
    const apiUrl = await _trainingApiUrl();
    const errEl = document.getElementById('training-error');
    if (errEl) errEl.classList.add('hidden');

    // Best-effort fixture-migration banner — non-blocking, errors swallowed.
    _refreshTrainingFixtureBanner(apiUrl).catch(() => {});

    try {
        const resp = await fetch(`${apiUrl}/calibration/plan`, {
            headers: _trainingAuthHeader(),
        });
        // Legacy: pre-2026-05-17 API returned 404 when no plan exists.
        // Current API returns 200 with `{plan: null}`. Accept both so
        // a mixed-version rollout doesn't break the panel.
        if (resp.status === 404) {
            _currentTrainingPlan = null;
            _renderTrainingState(null);
            _stopTrainingPoll();
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const body = await resp.json();
        // New shape `{plan: …}` vs legacy unwrapped plan object.
        const plan = body && typeof body === 'object' && 'plan' in body ? body.plan : body;
        if (!plan) {
            _currentTrainingPlan = null;
            _renderTrainingState(null);
            _stopTrainingPoll();
            return;
        }
        _currentTrainingPlan = plan;
        _renderTrainingState(plan);
        if (plan.status === 'running') {
            _startTrainingPoll();
        } else {
            _stopTrainingPoll();
        }
    } catch (err) {
        console.error('training state load failed:', err);
        if (errEl) {
            errEl.textContent = `Could not load training state: ${err.message || err}`;
            errEl.classList.remove('hidden');
        }
    }
}

function _renderTrainingState(plan) {
    const badge = document.getElementById('training-status-badge');
    const toggle = document.getElementById('training-enable-toggle');
    const slider = document.getElementById('training-target-slider');
    const valueEl = document.getElementById('training-target-value');
    const progressRow = document.getElementById('training-progress-row');
    const progressText = document.getElementById('training-progress-text');
    const tickText = document.getElementById('training-tick-text');
    const targetRow = document.getElementById('training-target-row');
    const restartBtn = document.getElementById('training-restart-btn');

    if (!badge || !toggle) return;

    if (plan === null || plan.status !== 'running') {
        const status = plan ? plan.status : 'off';
        // Plain-English mapping. Server statuses: off / completed /
        // cancelled / failed.
        const labelMap = {
            off: 'Off',
            completed: 'Done — all nodes learned',
            cancelled: 'Off (was cancelled)',
            failed: 'Stopped — error',
        };
        const label = labelMap[status] || status.charAt(0).toUpperCase() + status.slice(1);
        const colour = status === 'completed'
            ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
            : status === 'cancelled' || status === 'failed'
                ? 'bg-amber-50 text-amber-700 border-amber-200'
                : 'bg-gray-100 text-gray-600 border-gray-200';
        badge.className = `inline-block px-2 py-0.5 text-xs font-semibold rounded border ${colour}`;
        badge.textContent = label;
        toggle.checked = false;
        if (progressRow) progressRow.classList.add('hidden');
        if (slider) slider.disabled = false;
        if (targetRow) targetRow.classList.remove('opacity-50');
        // No plan running → nothing to clean. Hide the restart button.
        if (restartBtn) restartBtn.classList.add('hidden');
        return;
    }

    badge.className = 'inline-block px-2 py-0.5 text-xs font-semibold rounded border bg-blue-50 text-blue-700 border-blue-200';
    badge.textContent = 'Learning…';
    toggle.checked = true;
    if (slider) slider.value = plan.target_in_flight;
    if (valueEl) valueEl.textContent = String(plan.target_in_flight);
    if (slider) slider.disabled = true;
    if (targetRow) targetRow.classList.add('opacity-50');
    // Plan IS running — show the restart button so the operator can
    // flush and start over after shipping a fix.
    if (restartBtn) restartBtn.classList.remove('hidden');

    if (progressRow && progressText) {
        const total = plan.total_nodes || 0;
        const done = plan.nodes_completed || 0;
        const failed = plan.nodes_failed || 0;
        // Stacked-segment progress bar: emerald for done, amber for failed.
        // Both segments rendered as percentages of total, capped at 100% in
        // sum so a buggy server-side counter can't push past the bar's edge.
        const donePct = total > 0 ? Math.min(100, (done / total) * 100) : 0;
        const failedPct = total > 0 ? Math.min(100 - donePct, (failed / total) * 100) : 0;
        const doneBar = document.getElementById('training-progress-bar-done');
        const failedBar = document.getElementById('training-progress-bar-failed');
        if (doneBar) doneBar.style.width = `${donePct.toFixed(1)}%`;
        if (failedBar) failedBar.style.width = `${failedPct.toFixed(1)}%`;

        if (total > 0) {
            const pieces = [`${done} of ${total} nodes learned`];
            if (failed > 0) pieces.push(`${failed} failed`);
            progressText.textContent = pieces.join(' · ');
        } else {
            progressText.textContent = 'Just starting up…';
        }
        progressRow.classList.remove('hidden');
    }
    if (tickText) {
        tickText.textContent = _humanizeTickReason(plan.last_tick_reason);
    }
}

function _humanizeTickReason(reason) {
    if (!reason || typeof reason !== 'string') return '';
    const r = reason.trim();
    let m;
    if ((m = r.match(/^errored:\s*(.+)$/i))) {
        return `Something went wrong on the last check — ${m[1]}. The system will keep trying every 30 seconds.`;
    }
    if ((m = r.match(/^deferred:\s*in-flight\s+(\d+)\s*>=\s*target\s+(\d+)/i))) {
        // Don't say "paused" — workers are actively running these tests
        // right now. What's actually waiting is the dispatcher (the thing
        // that queues the *next* node's 84-task batch). Frame it that way
        // so the badge ("Learning…") and the status line don't contradict.
        return `${m[1]} tests running on the workers right now. Next node queues when this drops below ${m[2]}.`;
    }
    if ((m = r.match(/^deferred:\s*user backlog/i))) {
        return `User jobs are in the queue — letting them clear before adding more learning tests.`;
    }
    if ((m = r.match(/^deferred(:?\s*(.*))?$/i))) {
        return `Letting the current batch finish before queuing the next${m[2] ? ' — ' + m[2] : '.'}`;
    }
    if ((m = r.match(/^dispatched node\s+(\d+)(.*)$/i))) {
        return `Just queued tests for node ${m[1]}${m[2] || ''}.`;
    }
    if (/^all nodes complete/i.test(r)) {
        return `Done — every node has been tested.`;
    }
    return `Status: ${r}`;
}

async function _handleTrainingToggle(ev) {
    const toggle = ev.target;
    const errEl = document.getElementById('training-error');
    const slider = document.getElementById('training-target-slider');
    if (errEl) errEl.classList.add('hidden');
    toggle.disabled = true;

    try {
        if (toggle.checked) {
            await _enableTraining(slider ? parseInt(slider.value, 10) : 30);
        } else {
            await _disableTraining();
        }
        await _refreshTrainingState();
    } catch (err) {
        console.error('training toggle failed:', err);
        toggle.checked = !toggle.checked;
        if (errEl) {
            errEl.textContent = `Toggle failed: ${err.message || err}`;
            errEl.classList.remove('hidden');
        }
    } finally {
        toggle.disabled = false;
    }
}

async function _enableTraining(targetInFlight) {
    const apiUrl = await _trainingApiUrl();
    const resp = await fetch(`${apiUrl}/calibration/plan`, {
        method: 'POST',
        headers: { ..._trainingAuthHeader(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_in_flight: targetInFlight }),
    });
    if (!resp.ok) {
        const txt = await resp.text();
        throw new Error(`${resp.status}: ${txt}`);
    }
}

async function _disableTraining() {
    if (!_currentTrainingPlan || !_currentTrainingPlan.id) {
        return;  // nothing running, nothing to cancel
    }
    const apiUrl = await _trainingApiUrl();
    const resp = await fetch(
        `${apiUrl}/calibration/plan/${_currentTrainingPlan.id}/cancel`,
        { method: 'POST', headers: _trainingAuthHeader() },
    );
    if (!resp.ok) {
        const txt = await resp.text();
        throw new Error(`${resp.status}: ${txt}`);
    }
}

/**
 * Clean & restart handler. Cancels the running plan (which flushes
 * every PENDING calibration task — workers will stop pulling them
 * within one tick), then creates a fresh plan with the same
 * target_in_flight. Used after shipping a fixture or node-script
 * fix while a plan is mid-run: the in-flight tasks were enqueued
 * against the pre-fix code and would mostly fail. Restarting
 * dispatches everything fresh against the updated images.
 */
async function _handleTrainingRestart() {
    const btn = document.getElementById('training-restart-btn');
    const slider = document.getElementById('training-target-slider');
    const errEl = document.getElementById('training-error');
    if (!btn) return;

    if (!confirm(
        "Cancel the running plan and start a fresh one?\n\n" +
        "This drops every pending calibration task and re-dispatches " +
        "from zero against the latest images + fixtures. Use this " +
        "after shipping a fix while training was already running."
    )) {
        return;
    }

    btn.disabled = true;
    const prevLabel = btn.textContent;
    btn.textContent = 'Restarting…';
    if (errEl) errEl.classList.add('hidden');

    try {
        const apiUrl = await _trainingApiUrl();
        // Read current target from the slider (or fall back to 30, the
        // existing default the server enforces too).
        const target = slider ? parseInt(slider.value, 10) || 30 : 30;
        const resp = await fetch(`${apiUrl}/calibration/plan/restart`, {
            method: 'POST',
            headers: { ..._trainingAuthHeader(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_in_flight: target }),
        });
        if (!resp.ok) {
            const txt = await resp.text();
            throw new Error(`${resp.status}: ${txt}`);
        }
        // Pick up the new plan state immediately.
        await _refreshTrainingState();
    } catch (err) {
        console.error('training restart failed:', err);
        if (errEl) {
            errEl.textContent = `Restart failed: ${err.message || err}`;
            errEl.classList.remove('hidden');
        }
    } finally {
        btn.disabled = false;
        btn.textContent = prevLabel;
    }
}

function _startTrainingPoll() {
    if (_trainingPollHandle) return;
    _trainingPollHandle = setInterval(() => {
        _refreshTrainingState().catch(err => console.error('training poll failed:', err));
    }, 5000);
}

function _stopTrainingPoll() {
    if (_trainingPollHandle) {
        clearInterval(_trainingPollHandle);
        _trainingPollHandle = null;
    }
}

// ============================================================================
// δ §11 — per-worker concurrency slider. Reads + writes the
// worker_licenses.max_concurrent_tasks column via the existing
// `GET /platform/workers` and `PATCH /platform/workers/{name}/concurrency`
// endpoints (already platform-admin gated). The worker reads the value
// from the heartbeat response (worker change in commit 1835213c) so a
// slider change applies within ~30s without a worker restart.
//
// Single-slider UX assumption: if multiple workers are enrolled, the
// PATCH fans out across all of them so the slider value lands on every
// worker. That keeps the UX simple while the deployment is small. A
// per-worker breakdown can come when the fleet grows.
// ============================================================================

let _workerListCache = null;

async function _fetchWorkerList() {
    const apiUrl = await _trainingApiUrl();
    const resp = await fetch(`${apiUrl}/workers`, {
        headers: _trainingAuthHeader(),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
}

async function _refreshWorkerConcurrency() {
    const slider = document.getElementById('training-worker-concurrency-slider');
    const valueEl = document.getElementById('training-worker-concurrency-value');
    const targetEl = document.getElementById('training-worker-target-name');
    const statusEl = document.getElementById('training-worker-concurrency-status');
    if (!slider || !valueEl) return;

    if (statusEl) statusEl.classList.add('hidden');

    try {
        const workers = await _fetchWorkerList();
        _workerListCache = workers;

        if (!workers || workers.length === 0) {
            if (targetEl) targetEl.textContent = '(no enrolled workers found)';
            slider.disabled = true;
            valueEl.textContent = '—';
            return;
        }

        // Pick a value to display. If all workers agree, show that. If they
        // disagree, show the first one's value but flag the divergence in the
        // target label so the operator knows what's going on.
        const values = workers.map(w => Number(w.max_concurrent_tasks ?? 0));
        const allSame = values.every(v => v === values[0]);
        const display = values[0];
        slider.value = String(display);
        valueEl.textContent = display === 0 ? 'unlimited' : String(display);
        slider.disabled = false;

        if (targetEl) {
            const names = workers.map(w => w.worker_name).join(', ');
            targetEl.textContent = workers.length === 1
                ? names
                : `${names} (${workers.length} workers)${allSame ? '' : ' — values differ; setting will normalise'}`;
        }
    } catch (err) {
        console.error('worker concurrency refresh failed:', err);
        if (targetEl) targetEl.textContent = `(could not load workers: ${err.message || err})`;
        slider.disabled = true;
        valueEl.textContent = '—';
    }
}

async function _handleWorkerConcurrencyChange(ev) {
    const slider = ev.target;
    const valueEl = document.getElementById('training-worker-concurrency-value');
    const statusEl = document.getElementById('training-worker-concurrency-status');
    const newValue = parseInt(slider.value, 10);

    if (statusEl) statusEl.classList.add('hidden');
    slider.disabled = true;

    try {
        const workers = _workerListCache || (await _fetchWorkerList());
        if (!workers || workers.length === 0) {
            throw new Error('no enrolled workers');
        }

        const apiUrl = await _trainingApiUrl();
        // Fan out PATCH to every enrolled worker so the slider value lands
        // uniformly across the fleet. Errors are reported but don't roll
        // back partial successes — the operator can re-slide to retry.
        const results = await Promise.all(workers.map(async w => {
            const r = await fetch(
                `${apiUrl}/workers/${encodeURIComponent(w.worker_name)}/concurrency`,
                {
                    method: 'PATCH',
                    headers: { ..._trainingAuthHeader(), 'Content-Type': 'application/json' },
                    body: JSON.stringify({ max_concurrent_tasks: newValue }),
                },
            );
            return { worker: w.worker_name, ok: r.ok, status: r.status };
        }));

        const failures = results.filter(r => !r.ok);
        if (failures.length === 0) {
            if (statusEl) {
                statusEl.textContent = `Set to ${newValue === 0 ? 'unlimited' : newValue} on ${results.length} worker${results.length === 1 ? '' : 's'}. Takes effect within ~30 seconds.`;
                statusEl.className = 'text-xs text-emerald-700';
                statusEl.classList.remove('hidden');
            }
        } else {
            if (statusEl) {
                statusEl.textContent = `Set on ${results.length - failures.length} of ${results.length} workers; failed: ${failures.map(f => `${f.worker} (HTTP ${f.status})`).join(', ')}.`;
                statusEl.className = 'text-xs text-red-600';
                statusEl.classList.remove('hidden');
            }
        }

        if (valueEl) valueEl.textContent = newValue === 0 ? 'unlimited' : String(newValue);

        // Refresh cache so the next slider read sees current truth.
        _workerListCache = null;
    } catch (err) {
        console.error('worker concurrency PATCH failed:', err);
        if (statusEl) {
            statusEl.textContent = `Failed to apply: ${err.message || err}`;
            statusEl.className = 'text-xs text-red-600';
            statusEl.classList.remove('hidden');
        }
        // Re-read truth so the slider reflects what's actually persisted.
        _refreshWorkerConcurrency().catch(() => {});
    } finally {
        slider.disabled = false;
    }
}


// ============================================================================
// wizard-learning-from-feedback — Workflow Builder AI features sub-tab.
// Mirrors the Scheduler sub-tab style: one preset dropdown, a flat list
// of plain-language toggle rows, one Save button, Advanced collapsible.
// ============================================================================

const WL_FEATURES = [
    { key: 'rag', ready: true,
      label: 'Suggest from your team\'s past workflows',
      blurb: 'When users ask the wizard for help, show examples from your account\'s past successful workflows. Your team\'s data stays in your account.' },
    { key: 'rag_cross_tenant', ready: true,
      label: 'Suggest from common patterns across customers',
      blurb: 'Use generic workflow shapes from across the platform to help users who haven\'t built much yet. Only the structure (which node types connect) is shared — configurations and prompts stay private.' },
    { key: 'reranker', ready: false, trainable: true,
      label: 'Pick the best wizard answer',
      blurb: 'When the AI generates several possible workflows, automatically pick the one most likely to be useful based on past user feedback.' },
    { key: 'intent_classifier', ready: false, trainable: true,
      label: 'Better understand what users want',
      blurb: 'Replace the rule-based guess of what the user is asking for (start fresh? extend? audit?) with a learned classifier that catches unusual phrasings.' },
    { key: 'l2r', ready: false, trainable: true,
      label: 'Order suggestions by usefulness',
      blurb: 'When the wizard offers several "next step" nodes, sort them by which ones users actually picked in similar situations before.' },
    { key: 'audit_triage', ready: false, trainable: true,
      label: 'Hide unhelpful audit warnings',
      blurb: 'When the wizard\'s audit feature flags issues, suppress findings that users usually dismiss. Less noise, more signal.' },
];

// Style presets — flips toggles in bulk. Mirrors Scheduler tab pattern.
const WL_PRESETS = {
    off:        { rag: false, rag_cross_tenant: false, reranker: false, intent_classifier: false, l2r: false, audit_triage: false },
    balanced:   { rag: true,  rag_cross_tenant: true,  reranker: false, intent_classifier: false, l2r: false, audit_triage: false },
    aggressive: { rag: true,  rag_cross_tenant: true,  reranker: true,  intent_classifier: true,  l2r: true,  audit_triage: true  },
};

let _wizardLearningWired = false;
let _wizardLearningRemoteCfg = null;
let _wizardLearningRemoteThresholds = null;

async function _initWizardLearningSubTab() {
    // rc20-s20 — the Wizard Learning sub-tab's whole backend is the wizard-ml
    // service: services/wizard_ml/** is DENIED on CE (open-core/manifest.yaml
    // deny:), so main.py's try-mount of routers/wizard_ml_admin.py (:2970) is
    // skipped and every /system/wizard-ml/* read 404s. Same gate/pattern as
    // _initResourceMonitorSubTab (:6798): hide the tab and never fetch it on
    // CE; fail-open (unreadable entitlement = paid), so paid keeps the old
    // behaviour untouched.
    if (!await isPaidEdition()) {
        const hiddenBtn = document.getElementById('ai-tool-tab-wizard-learning');
        if (hiddenBtn) {
            hiddenBtn.classList.add('hidden');
            // FIX-B57: same deliberate-hide marker as _initAgentSubTab — without it the
            // _applyMasterMode reveal pass un-hides this paid-only sub-tab on CE, where
            // its whole backend (services/wizard_ml/**) 404s.
            hiddenBtn.dataset.deliberateHide = '1';
        }
        return;
    }
    const tabBtn = document.getElementById('ai-tool-tab-wizard-learning');
    if (tabBtn) tabBtn.classList.remove('hidden');
    if (_wizardLearningWired) {
        _wlReloadAll().catch(err => console.error('wizard-learning reload:', err));
        return;
    }
    _wizardLearningWired = true;
    document.getElementById('wizard-learning-save')?.addEventListener('click', _wlSave);
    document.getElementById('wizard-learning-revert')?.addEventListener('click', _wlRevert);
    document.getElementById('wizard-learning-refresh-registry')?.addEventListener('click', _wlRefreshRegistry);
    document.getElementById('wizard-learning-purge-synthetic')?.addEventListener('click', _wlPurgeSynthetic);
    document.getElementById('wizard-learning-generate-synthetic')?.addEventListener('click', _wlGenerateSynthetic);
    document.getElementById('wizard-learning-allow-synthetic')?.addEventListener('change', _wlSaveAllowSynthetic);
    document.getElementById('wizard-learning-llm-pin')?.addEventListener('input', _wlMarkDirty);
    document.getElementById('wizard-learning-preset')?.addEventListener('change', _wlOnPresetChange);
    _wlReloadAll().catch(err => console.error('wizard-learning init:', err));
}

function _wlApiUrl()     { return window.APP_CONFIG?.API_URL || ''; }
function _wlAuthHeader() {
    const t = sessionStorage.getItem('accessToken');
    return t ? { 'Authorization': `Bearer ${t}` } : {};
}

function _wlSetError(msg) {
    const el = document.getElementById('wizard-learning-error');
    if (!el) return;
    if (msg) { el.textContent = msg; el.classList.remove('hidden'); } else el.classList.add('hidden');
}

function _wlSetStatus(msg) {
    const el = document.getElementById('wizard-learning-status');
    if (!el) return;
    if (msg) { el.textContent = msg; el.classList.remove('hidden'); setTimeout(() => el.classList.add('hidden'), 4000); }
    else el.classList.add('hidden');
}

function _wlMarkDirty() {
    document.getElementById('wizard-learning-dirty-hint')?.classList.remove('hidden');
    _wlRefreshPresetIndicator();
}

function _wlMarkClean() {
    document.getElementById('wizard-learning-dirty-hint')?.classList.add('hidden');
}

async function _wlReloadAll() {
    _wlSetError('');
    await Promise.all([_wlLoadConfigAndThresholds(), _wlLoadRegistry()]);
    _wlRenderFeatureRows();
    _wlRenderCutoffSliders();
    _wlRefreshPresetIndicator();
    _wlMarkClean();
    // If a training run is already in flight (e.g., page reload mid-train),
    // pick up the polling so the user sees the progress bar resume.
    try {
        const resp = await fetch(`${_wlApiUrl()}/system/wizard-ml/training-status`, { headers: _wlAuthHeader() });
        if (resp.ok) {
            const data = await resp.json();
            const live = (data.entries || []).find(e => e.is_running);
            if (live) _wlPollTrainingStatus(live.model_kind);
        }
    } catch (_) { /* non-fatal */ }
}

async function _wlLoadConfigAndThresholds() {
    const [cfgResp, thrResp] = await Promise.all([
        fetch(`${_wlApiUrl()}/system/wizard-ml/config`,          { headers: _wlAuthHeader() }),
        fetch(`${_wlApiUrl()}/system/wizard-ml/data-thresholds`, { headers: _wlAuthHeader() }),
    ]);
    if (!cfgResp.ok) { _wlSetError(`Could not load AI settings: HTTP ${cfgResp.status}`); return; }
    _wizardLearningRemoteCfg = await cfgResp.json();
    if (thrResp.ok) {
        const data = await thrResp.json();
        _wizardLearningRemoteThresholds = (data.thresholds || []).reduce((m, t) => { m[t.model_kind] = t; return m; }, {});
    } else {
        _wizardLearningRemoteThresholds = {};
    }
}

async function _wlLoadRegistry() {
    const root = document.getElementById('wizard-learning-registry');
    if (!root) return;
    const resp = await fetch(`${_wlApiUrl()}/system/wizard-ml/registry`, { headers: _wlAuthHeader() });
    if (!resp.ok) { root.innerHTML = `<p class="text-xs text-red-600">Failed to load (HTTP ${resp.status}).</p>`; return; }
    const data = await resp.json();
    const entries = data.entries || [];
    if (entries.length === 0) {
        root.innerHTML = `<p class="text-xs text-gray-500 italic">No models loaded yet. Models appear once a feature has collected enough feedback and trained.</p>`;
        return;
    }
    const labels = WL_FEATURES.reduce((m, f) => { m[f.key] = f.label; return m; }, {});
    const rows = entries.map(e => `
        <tr class="border-t border-gray-100">
            <td class="py-1.5 text-gray-700">${labels[e.model_kind] || e.model_kind}</td>
            <td class="py-1.5 font-mono text-xs text-gray-600">${e.version || '—'}</td>
            <td class="py-1.5 text-gray-600">${e.eval_metric != null ? Number(e.eval_metric).toFixed(4) : '—'}</td>
            <td class="py-1.5 text-gray-500 text-xs">${e.deployed_at || '—'}</td>
        </tr>
    `).join('');
    root.innerHTML = `<table class="w-full text-sm"><thead><tr class="text-xs text-gray-500"><th class="text-left py-1.5">Feature</th><th class="text-left py-1.5">Version</th><th class="text-left py-1.5">Quality score</th><th class="text-left py-1.5">Trained</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function _wlFeatureStatus(f, cfg, threshold) {
    const enabled = !!(cfg.toggles && cfg.toggles[f.key]);
    if (f.ready) {
        return enabled
            ? { label: 'On',  cls: 'bg-green-100 text-green-800 border-green-200' }
            : { label: 'Off', cls: 'bg-gray-100  text-gray-600  border-gray-200'  };
    }
    if (enabled) return { label: 'On', cls: 'bg-green-100 text-green-800 border-green-200' };
    if (threshold && threshold.ready) return { label: 'Ready to train', cls: 'bg-blue-100 text-blue-800 border-blue-200' };
    return { label: 'Collecting feedback', cls: 'bg-amber-100 text-amber-800 border-amber-200' };
}

function _wlRenderFeatureRows() {
    const root = document.getElementById('wizard-learning-feature-rows');
    if (!root || !_wizardLearningRemoteCfg) return;
    const cfg = _wizardLearningRemoteCfg;
    root.innerHTML = '';
    // rag_cross_tenant_available comes from the API (false on MOD SaaS,
    // true on on-prem). On SaaS the control is locked off server-side,
    // so it renders disabled with an explanation instead of a working
    // toggle.
    const crossTenantAvailable = cfg.rag_cross_tenant_available !== false;
    for (const f of WL_FEATURES) {
        const threshold = _wizardLearningRemoteThresholds?.[f.key];
        const lockedOff = f.key === 'rag_cross_tenant' && !crossTenantAvailable;
        const status = _wlFeatureStatus(f, cfg, threshold);
        const enabled = lockedOff ? false : !!(cfg.toggles && cfg.toggles[f.key]);
        const canEnable = (f.ready || (threshold && threshold.ready)) && !lockedOff;
        const toggleAttrs = canEnable ? '' : (lockedOff
            ? 'disabled title="Not available on MOD SaaS; each workspace learns only from its own data."'
            : 'disabled title="Needs more user feedback before it can turn on."');

        let progressBlock = '';
        if (!f.ready && threshold) {
            const pct = Math.min(100, Math.round((threshold.current / Math.max(1, threshold.required)) * 100));
            progressBlock = `
                <div class="mt-2 flex items-center gap-2 text-xs text-gray-500 max-w-md">
                    <div class="flex-1 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                        <div class="h-full ${threshold.ready ? 'bg-green-500' : 'bg-amber-400'}" style="width:${pct}%"></div>
                    </div>
                    <span class="font-mono shrink-0">${threshold.current.toLocaleString()} / ${threshold.required.toLocaleString()}</span>
                </div>
            `;
        }

        let trainLink = '';
        if (f.trainable) {
            const ready = threshold && threshold.ready;
            trainLink = ready
                ? `<button type="button" data-wl-retrain="${f.key}" class="text-xs text-primary hover:underline mt-1">Train now</button>`
                : '';
        }

        // Deployment-mode note for the cross-tenant toggle: an
        // explanation when SaaS locks it off, a scope warning when
        // on-prem turns it on.
        let availabilityNote = '';
        if (f.key === 'rag_cross_tenant') {
            if (lockedOff) {
                availabilityNote = `<p class="text-xs text-gray-400 mt-1">Not available on MOD SaaS; each workspace learns only from its own data.</p>`;
            } else if (enabled) {
                availabilityNote = `<p class="text-xs text-amber-700 mt-1">When on, anonymised wizard results from every tenant on this install help improve suggestions for all tenants on this install.</p>`;
            }
        }

        const row = document.createElement('div');
        row.className = 'flex items-start gap-4';
        row.innerHTML = `
            <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                    <span class="text-sm font-medium text-gray-700">${f.label}</span>
                    <span class="inline-block px-2 py-0.5 text-xs font-medium rounded border ${status.cls}">${status.label}</span>
                </div>
                <p class="text-xs text-gray-500 mt-1">${f.blurb}</p>
                ${availabilityNote}
                ${progressBlock}
                ${trainLink}
            </div>
            <label class="inline-flex items-center cursor-pointer shrink-0 mt-0.5">
                <input data-wl-toggle="${f.key}" aria-label="${f.label}" type="checkbox" class="sr-only peer" ${enabled ? 'checked' : ''} ${toggleAttrs}>
                <div class="relative w-11 h-6 bg-gray-300 rounded-full peer-checked:bg-primary peer-focus:ring-2 peer-focus:ring-primary/30 transition-colors after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:after:translate-x-5 peer-disabled:opacity-50 peer-disabled:cursor-not-allowed"></div>
            </label>
        `;
        root.appendChild(row);
        row.querySelector(`[data-wl-toggle="${f.key}"]`)?.addEventListener('change', () => _wlMarkDirty());
        row.querySelector(`[data-wl-retrain="${f.key}"]`)?.addEventListener('click', () => _wlTriggerRetrain(f.key, f.label));
    }
    const pinEl = document.getElementById('wizard-learning-llm-pin');
    if (pinEl) pinEl.value = (_wizardLearningRemoteCfg.llm_model_pin || '');
    const allowEl = document.getElementById('wizard-learning-allow-synthetic');
    if (allowEl) allowEl.checked = !!(_wizardLearningRemoteCfg.dev_flags?.allow_synthetic_models);
}

function _wlRenderCutoffSliders() {
    const root = document.getElementById('wizard-learning-cutoffs');
    if (!root || !_wizardLearningRemoteCfg) return;
    const cfg = _wizardLearningRemoteCfg;
    root.innerHTML = '';
    for (const f of WL_FEATURES) {
        if (f.key === 'rag_cross_tenant') continue;  // shares 'rag' bucket; one slider is plenty
        const cutoff = (cfg.ab_cutoffs && cfg.ab_cutoffs[f.key] != null) ? cfg.ab_cutoffs[f.key] : 50;
        const row = document.createElement('div');
        row.className = 'space-y-1';
        row.innerHTML = `
            <div class="flex items-baseline justify-between">
                <label class="text-sm text-gray-700" for="wl-cutoff-${f.key}">${f.label}</label>
                <span class="text-sm font-mono text-gray-800"><span data-wl-cutoff-display="${f.key}">${cutoff}</span>%</span>
            </div>
            <input id="wl-cutoff-${f.key}" data-wl-cutoff="${f.key}" type="range" min="0" max="100" step="1" value="${cutoff}" class="w-full">
        `;
        root.appendChild(row);
        const slider = row.querySelector(`[data-wl-cutoff="${f.key}"]`);
        const disp = row.querySelector(`[data-wl-cutoff-display="${f.key}"]`);
        slider?.addEventListener('input', () => { disp.textContent = slider.value; _wlMarkDirty(); });
    }
}

function _wlCurrentToggles() {
    const out = {};
    for (const f of WL_FEATURES) {
        const t = document.querySelector(`[data-wl-toggle="${f.key}"]`);
        out[f.key] = t ? !!t.checked : false;
    }
    return out;
}

function _wlMatchesPreset(toggles, preset) {
    return Object.keys(preset).every(k => preset[k] === !!toggles[k]);
}

function _wlRefreshPresetIndicator() {
    const sel = document.getElementById('wizard-learning-preset');
    if (!sel) return;
    const toggles = _wlCurrentToggles();
    let match = 'custom';
    for (const [name, preset] of Object.entries(WL_PRESETS)) {
        if (_wlMatchesPreset(toggles, preset)) { match = name; break; }
    }
    sel.value = match;
    const customOpt = sel.querySelector('option[value="custom"]');
    if (customOpt) {
        customOpt.disabled = (match !== 'custom');
        customOpt.hidden   = (match !== 'custom');
    }
}

function _wlOnPresetChange(e) {
    const preset = WL_PRESETS[e.target.value];
    if (!preset) return;
    for (const f of WL_FEATURES) {
        const t = document.querySelector(`[data-wl-toggle="${f.key}"]`);
        if (!t) continue;
        if (t.disabled) { t.checked = false; continue; }
        t.checked = !!preset[f.key];
    }
    _wlMarkDirty();
}

async function _wlRefreshRegistry() {
    _wlSetError('');
    const resp = await fetch(`${_wlApiUrl()}/system/wizard-ml/refresh-registry`, { method: 'POST', headers: _wlAuthHeader() });
    if (resp.ok) { _wlSetStatus('Model list refreshed.'); await _wlLoadRegistry(); }
    else _wlSetError(`Refresh failed: HTTP ${resp.status}`);
}

async function _wlSaveAllowSynthetic(e) {
    _wlSetError('');
    const checked = !!e.target.checked;
    const resp = await fetch(`${_wlApiUrl()}/system/wizard-ml/config`, {
        method: 'PUT',
        headers: { ..._wlAuthHeader(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ dev_flags: { allow_synthetic_models: checked } }),
    });
    if (!resp.ok) {
        e.target.checked = !checked;
        const msg = (await resp.json().catch(() => ({})))?.detail || `HTTP ${resp.status}`;
        _wlSetError(`Could not save: ${msg}`);
        return;
    }
    _wizardLearningRemoteCfg = await resp.json();
    _wlSetStatus(checked
        ? 'Synthetic models allowed. Trained synthetic artifacts will load on the next registry refresh.'
        : 'Synthetic models blocked. The registry will skip any synthetic artifacts on the next refresh.');
    await _wlLoadRegistry();
}

async function _wlGenerateSynthetic() {
    _wlSetError('');
    const ok = window.confirm(
        'Generate synthetic test data?\n\n' +
        'Writes ~1,600 fake feedback events tagged "synthetic" (kept apart ' +
        'from real production data) and starts training the reranker model. ' +
        'Training takes ~60s and downloads ~80MB the first time.\n\n' +
        'Use the "Allow synthetic-trained models to load" checkbox to actually ' +
        'serve the trained model. Click Delete synthetic test data when done.'
    );
    if (!ok) return;

    const btn = document.getElementById('wizard-learning-generate-synthetic');
    const out = document.getElementById('wizard-learning-generate-result');
    if (btn) btn.disabled = true;
    if (out) { out.classList.add('hidden'); out.textContent = ''; }
    try {
        const resp = await fetch(`${_wlApiUrl()}/system/wizard-ml/generate-synthetic`, {
            method: 'POST',
            headers: { ..._wlAuthHeader(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ n_accepted: 1200, n_rejected: 400, seed: 42, auto_train: true /* tenant_id omitted = use caller's tenant */ }),
        });
        if (!resp.ok) {
            const msg = (await resp.json().catch(() => ({})))?.detail || `HTTP ${resp.status}`;
            _wlSetError(`Generation failed: ${msg}`);
            return;
        }
        const r = await resp.json();
        if (out) {
            out.textContent = r.detail;
            out.classList.remove('hidden');
        }
        _wlSetStatus(r.training_thread_started
            ? 'Synthetic data written. Training started — see progress below.'
            : 'Synthetic data written.');
        if (r.training_thread_started) {
            _wlPollTrainingStatus('reranker');
            _wlPollRegistryUntilLoaded(30);  // up to 5 min — first run downloads the model
        }
        await _wlLoadRegistry();
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function _wlPollRegistryUntilLoaded(maxAttempts) {
    for (let i = 0; i < maxAttempts; i++) {
        await new Promise(r => setTimeout(r, 10000));
        try {
            const resp = await fetch(`${_wlApiUrl()}/system/wizard-ml/refresh-registry`, { method: 'POST', headers: _wlAuthHeader() });
            if (resp.ok) await _wlLoadRegistry();
            const reg = await fetch(`${_wlApiUrl()}/system/wizard-ml/registry`, { headers: _wlAuthHeader() });
            if (reg.ok) {
                const data = await reg.json();
                if ((data.entries || []).some(e => e.model_kind === 'reranker' && e.version)) return;
            }
        } catch (_) { /* keep polling */ }
    }
}

const _WL_PHASE_LABEL = {
    idle:              'Idle',
    starting:          'Starting',
    assembling_data:   'Assembling training data',
    loading_model:     'Loading base model (downloading on first run)',
    training:          'Training',
    saving:            'Saving model',
    uploading:         'Uploading to storage',
    completed:         'Completed',
    failed:            'Failed',
};

let _wlTrainingPollHandle = null;

function _wlShowTrainingProgress(snapshot) {
    const wrap = document.getElementById('wizard-learning-training-progress');
    if (!wrap) return;
    if (!snapshot || (!snapshot.is_running && snapshot.phase !== 'completed' && snapshot.phase !== 'failed')) {
        wrap.classList.add('hidden');
        return;
    }
    wrap.classList.remove('hidden');
    const phaseEl = document.getElementById('wizard-learning-training-phase');
    const elapsedEl = document.getElementById('wizard-learning-training-elapsed');
    const barEl = document.getElementById('wizard-learning-training-bar');
    const detailEl = document.getElementById('wizard-learning-training-detail');
    if (phaseEl) phaseEl.textContent = _WL_PHASE_LABEL[snapshot.phase] || snapshot.phase;
    if (elapsedEl) elapsedEl.textContent = `${snapshot.elapsed_seconds}s`;
    if (barEl) barEl.style.width = `${Math.round((snapshot.progress_fraction || 0) * 100)}%`;
    if (detailEl) {
        if (snapshot.phase === 'failed') {
            detailEl.textContent = snapshot.error || snapshot.detail || 'Training failed.';
            detailEl.className = 'text-xs text-red-700 mt-2';
        } else if (snapshot.phase === 'completed') {
            const v = snapshot.artifact_version ? ` (${snapshot.artifact_version})` : '';
            detailEl.textContent = `${snapshot.detail || 'Training complete'}${v}`;
            detailEl.className = 'text-xs text-green-700 mt-2 font-medium';
        } else {
            detailEl.textContent = snapshot.detail || '';
            detailEl.className = 'text-xs text-amber-800 mt-2';
        }
    }
}

async function _wlPollTrainingStatus(modelKind) {
    if (_wlTrainingPollHandle) return; // already polling
    const tick = async () => {
        try {
            const resp = await fetch(`${_wlApiUrl()}/system/wizard-ml/training-status`, { headers: _wlAuthHeader() });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            const snap = (data.entries || []).find(e => e.model_kind === modelKind);
            _wlShowTrainingProgress(snap);
            if (snap && (snap.phase === 'completed' || snap.phase === 'failed')) {
                clearInterval(_wlTrainingPollHandle);
                _wlTrainingPollHandle = null;
                await _wlLoadRegistry();
                // Leave the success/failure card visible for ~10s so the user can read it.
                setTimeout(() => _wlShowTrainingProgress(null), 10000);
            }
        } catch (_) { /* keep polling — transient errors OK */ }
    };
    await tick();
    _wlTrainingPollHandle = setInterval(tick, 2000);
}

async function _wlPurgeSynthetic() {
    _wlSetError('');
    const ok = window.confirm(
        'Delete all synthetic test data?\n\n' +
        'Removes feedback events, embeddings, and AI model artifacts ' +
        'tagged as synthetic. Real production data is not touched.\n\n' +
        'This cannot be undone.'
    );
    if (!ok) return;

    const btn = document.getElementById('wizard-learning-purge-synthetic');
    const out = document.getElementById('wizard-learning-purge-result');
    if (btn) btn.disabled = true;
    if (out) { out.classList.add('hidden'); out.textContent = ''; }
    try {
        const resp = await fetch(`${_wlApiUrl()}/system/wizard-ml/purge-synthetic`, {
            method: 'POST', headers: _wlAuthHeader(),
        });
        if (!resp.ok) {
            const msg = (await resp.json().catch(() => ({})))?.detail || `HTTP ${resp.status}`;
            _wlSetError(`Purge failed: ${msg}`);
            return;
        }
        const r = await resp.json();
        const summary = (
            `Deleted ${r.events_deleted} feedback event${r.events_deleted === 1 ? '' : 's'}, ` +
            `${r.vector_shared_rows_deleted} cross-tenant embedding${r.vector_shared_rows_deleted === 1 ? '' : 's'}, ` +
            `${r.vector_tenant_tables_dropped} per-tenant embedding table${r.vector_tenant_tables_dropped === 1 ? '' : 's'}, ` +
            `${r.minio_versions_deleted} model version${r.minio_versions_deleted === 1 ? '' : 's'} ` +
            `(${r.minio_objects_deleted} object${r.minio_objects_deleted === 1 ? '' : 's'}).`
        );
        if (out) {
            out.textContent = summary;
            out.classList.remove('hidden');
        }
        _wlSetStatus('Synthetic test data deleted.');
        await _wlLoadRegistry();
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function _wlTriggerRetrain(kind, label) {
    _wlSetError('');
    const resp = await fetch(`${_wlApiUrl()}/system/wizard-ml/train`, {
        method: 'POST',
        headers: { ..._wlAuthHeader(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_kind: kind, scope: 'baseline' }),
    });
    if (resp.ok) _wlSetStatus(`Started training "${label}" in the background.`);
    else {
        const msg = (await resp.json().catch(() => ({})))?.detail || `HTTP ${resp.status}`;
        _wlSetError(`Could not start training: ${msg}`);
    }
}

async function _wlSave() {
    _wlSetError('');
    const toggles = _wlCurrentToggles();
    const ab_cutoffs = {};
    document.querySelectorAll('[data-wl-cutoff]').forEach(el => {
        ab_cutoffs[el.dataset.wlCutoff] = parseInt(el.value, 10);
    });
    const pinEl = document.getElementById('wizard-learning-llm-pin');
    const llm_model_pin = pinEl ? (pinEl.value.trim() || null) : null;
    const btn = document.getElementById('wizard-learning-save');
    if (btn) btn.disabled = true;
    try {
        const resp = await fetch(`${_wlApiUrl()}/system/wizard-ml/config`, {
            method: 'PUT',
            headers: { ..._wlAuthHeader(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ toggles, ab_cutoffs, llm_model_pin }),
        });
        if (!resp.ok) { _wlSetError(`Save failed: HTTP ${resp.status}`); return; }
        _wizardLearningRemoteCfg = await resp.json();
        _wlRenderFeatureRows();
        _wlRenderCutoffSliders();
        _wlRefreshPresetIndicator();
        _wlMarkClean();
        _wlSetStatus('Saved. Changes take effect within a minute.');
    } finally {
        if (btn) btn.disabled = false;
    }
}

function _wlRevert() {
    if (!_wizardLearningRemoteCfg) return;
    _wlRenderFeatureRows();
    _wlRenderCutoffSliders();
    _wlRefreshPresetIndicator();
    _wlMarkClean();
    _wlSetStatus('Reverted to last saved settings.');
}


// ============================================================================
// tenant-resource-monitor §6 — Resource Monitor sub-tab.
// Same pattern as the Scheduler sub-tab: idempotent reveal + GET on entry +
// PUT on save. Platform-admin only (main-tab-ai is already admin-gated).
// ============================================================================

let _resourceMonitorSubTabWired = false;

// KP.8 — Node Wizard Knowledge Pack sub-tab. "Check for updates now" and
// "Upload pack file" both call the already-shipped POST
// /wizard/update-knowledge (network path vs. air-gap pack_bytes_b64 path);
// there is no GET endpoint yet for current version/history (deferred), so
// the raw response from either action is the only status surface. The
// auto-update toggle reads/writes the generic platform-settings endpoints
// and hides its row entirely if the setting key isn't registered (404).
let _wizardKnowledgePackWired = false;

function _wkpEscapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _wkpSetError(msg) {
    const el = document.getElementById('wizard-knowledge-pack-error');
    if (!el) return;
    if (msg) { el.innerHTML = _wkpEscapeHtml(msg); el.classList.remove('hidden'); }
    else el.classList.add('hidden');
}

function _wkpSetStatus(msg) {
    const el = document.getElementById('wizard-knowledge-pack-status');
    if (!el) return;
    if (msg) { el.innerHTML = _wkpEscapeHtml(msg); el.classList.remove('hidden'); setTimeout(() => el.classList.add('hidden'), 4000); }
    else el.classList.add('hidden');
}

function _wkpRenderResult(obj) {
    const el = document.getElementById('wizard-knowledge-pack-result');
    if (!el) return;
    el.textContent = JSON.stringify(obj, null, 2);
}

function _wkpSummarize(resp) {
    const parts = [];
    if (resp.version_applied) parts.push(`version ${_wkpEscapeHtml(resp.version_applied)} applied`);
    if (typeof resp.rows_upserted === 'number') parts.push(`${resp.rows_upserted} rows upserted`);
    if (resp.integrity_status) parts.push(`status: ${_wkpEscapeHtml(resp.integrity_status)}`);
    return parts.join(', ') || 'Done.';
}

async function _wkpLoadAutoUpdate() {
    const row = document.getElementById('wizard-knowledge-pack-auto-update-row');
    const checkbox = document.getElementById('wizard-knowledge-pack-auto-update');
    if (!row || !checkbox) return;
    // rc20-s20 — the KP.7 auto-update setting is psr-registered by
    // services/wizard_knowledge_settings.py, which open-core/manifest.yaml
    // denies on CE (deny:, :1246), so GET /platform/settings/<key> 404s there
    // (unknown key -> 404, routers/platform_settings.py:457). Hide the row and
    // never fetch it — the exact degrade the catch below already performs;
    // paid keeps the old behaviour (fail-open on unreadable entitlement).
    if (!await isPaidEdition()) {
        row.classList.add('hidden');
        return;
    }
    try {
        const data = await apiCall('/platform/settings/wizard_knowledge_auto_update_enabled');
        checkbox.checked = !!(data && data.value);
        row.classList.remove('hidden');
    } catch (err) {
        // KP.7's setting isn't registered yet (or any other fetch
        // failure) -- degrade gracefully by hiding the toggle row
        // rather than surfacing an error for a feature that may not
        // exist yet.
        row.classList.add('hidden');
    }
}

async function _wkpOnAutoUpdateChange(e) {
    const checked = e.target.checked;
    try {
        await apiCall('/platform/settings/wizard_knowledge_auto_update_enabled', {
            method: 'PATCH',
            body: JSON.stringify({ value: checked }),
        });
        _wkpSetStatus('Auto-update setting saved.');
    } catch (err) {
        e.target.checked = !checked;
        _wkpSetError(err?.message || 'Failed to save auto-update setting.');
    }
}

async function _wkpRunUpdate(body, busyBtn) {
    _wkpSetError('');
    if (busyBtn) busyBtn.disabled = true;
    try {
        const resp = await apiCall('/wizard/update-knowledge', {
            method: 'POST',
            body: JSON.stringify(body),
        });
        _wkpRenderResult(resp);
        _wkpSetStatus(_wkpSummarize(resp));
    } catch (err) {
        // 409 (manifest_unreachable / signature_invalid / checksum_mismatch /
        // downgrade_rejected / replay_rejected) and 422 (integrity_check_failed)
        // both carry {"detail": "<reason>", "rejection_reasons": [...]}
        // as err.detail (ApiError's parsed JSON body's `detail` field).
        const d = err?.detail;
        if (d && typeof d === 'object') {
            _wkpRenderResult(d);
            const reasons = Array.isArray(d.rejection_reasons) ? d.rejection_reasons.join(', ') : '';
            _wkpSetError(`${_wkpEscapeHtml(d.detail || 'Update rejected')}${reasons ? ': ' + _wkpEscapeHtml(reasons) : ''}`);
        } else {
            _wkpSetError(err?.message || 'Update failed.');
        }
    } finally {
        if (busyBtn) busyBtn.disabled = false;
    }
}

function _wkpCheckNow(e) {
    _wkpRunUpdate({}, e.target);
}

function _wkpReadFileAsBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
            // reader.result is a data: URL ("data:<mime>;base64,<data>") --
            // strip the prefix to get the raw base64 payload the endpoint
            // expects in pack_bytes_b64.
            const result = String(reader.result || '');
            const comma = result.indexOf(',');
            resolve(comma >= 0 ? result.slice(comma + 1) : result);
        };
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
    });
}

async function _wkpUploadPackFile(e) {
    const fileInput = document.getElementById('wizard-knowledge-pack-file');
    const file = fileInput?.files?.[0];
    if (!file) {
        _wkpSetError('Choose a pack file first.');
        return;
    }
    try {
        const pack_bytes_b64 = await _wkpReadFileAsBase64(file);
        await _wkpRunUpdate({ pack_bytes_b64 }, e.target);
    } catch (err) {
        _wkpSetError(err?.message || 'Failed to read pack file.');
    }
}

function _initWizardKnowledgePackSubTab() {
    const tabBtn = document.getElementById('ai-tool-tab-wizard-knowledge-pack');
    if (tabBtn) tabBtn.classList.remove('hidden');
    if (_wizardKnowledgePackWired) {
        _wkpLoadAutoUpdate().catch(err => console.error('wizard-knowledge-pack reload:', err));
        return;
    }
    _wizardKnowledgePackWired = true;
    document.getElementById('wizard-knowledge-pack-check-now')?.addEventListener('click', _wkpCheckNow);
    document.getElementById('wizard-knowledge-pack-upload')?.addEventListener('click', _wkpUploadPackFile);
    document.getElementById('wizard-knowledge-pack-auto-update')?.addEventListener('change', _wkpOnAutoUpdateChange);
    _wkpLoadAutoUpdate().catch(err => console.error('wizard-knowledge-pack init:', err));
}

// =========================================================================
// KEEPWARM-UI (owner Tim 11-25: "that isn't a place") — Settings home for
// the two KEEP-WARM-SLICE-1 switches (services/platform_settings.py):
//   worker.warm_pool_enabled  — TENANT switch (default false). The generic
//     platform-settings GET/PATCH stamp a tenant row when tenant_id is given,
//     so the toggle reads/writes THIS tenant's resolved value.
//   worker.warm_pool_allowed  — PLATFORM kill switch (default true), platform
//     scope only (no tenant_id on the PATCH).
// Platform-admin only: the whole AI Config tab is already platform-admin-only
// (settings.js hides main-tab-ai), and this init re-checks is_platform_admin
// defensively — the backend (_require_platform_admin on PATCH) is the real
// boundary. Both keys ship in CE unchanged (KEEP-WARM-SLICE-1: same keys and
// defaults in CE and paid, no licence gate, no quantity cap), so there is NO
// edition gate here — unlike the KP.7 auto-update row, whose setting is CE-
// denied. A row still hides if its key 404s (unknown key), same degrade as
// the KP.8 auto-update toggle, so a build without the slice shows nothing.
// =========================================================================

let _keepWarmWired = false;

function _kwSetError(msg) {
    const el = document.getElementById('keep-warm-error');
    if (!el) return;
    if (msg) { el.innerHTML = _wkpEscapeHtml(msg); el.classList.remove('hidden'); }
    else el.classList.add('hidden');
}

function _kwSetStatus(msg) {
    const el = document.getElementById('keep-warm-status');
    if (!el) return;
    if (msg) { el.innerHTML = _wkpEscapeHtml(msg); el.classList.remove('hidden'); setTimeout(() => el.classList.add('hidden'), 4000); }
    else el.classList.add('hidden');
}

function _kwTenantParam() {
    // This tenant's id, for the tenant-scoped GET/PATCH of the tenant switch.
    // No tenant (platform-scope users) -> omit: the API then resolves the
    // platform value, and the PATCH writes platform scope, which is exactly
    // what a platform admin means by "this tenant" when there is none.
    // KEEPWARM-TENANT-HIDE (owner Tim 11-25): the UI no longer relies on this
    // fallback — the tenant row is hidden and its PATCH never fires when the
    // signed-in user has no tenant_id (see _kwHasTenant).
    const tid = currentUser && currentUser.tenant_id;
    return (tid === null || tid === undefined) ? '' : `?tenant_id=${encodeURIComponent(tid)}`;
}

function _kwHasTenant() {
    // KEEPWARM-TENANT-HIDE (owner Tim 11-25: "hide the tenant switch for a
    // platform admin who has no tenant"): true only when the signed-in user
    // carries a non-empty tenant_id. Without one the tenant row stays hidden
    // and no tenant GET/PATCH is ever sent — a platform-scope admin has no
    // "this tenant" to switch.
    const tid = currentUser && currentUser.tenant_id;
    return tid !== null && tid !== undefined && tid !== '';
}

async function _kwLoadRows() {
    _kwSetError('');
    const tenantRow = document.getElementById('keep-warm-tenant-row');
    const tenantCb = document.getElementById('keep-warm-tenant-enabled');
    const platformRow = document.getElementById('keep-warm-platform-row');
    const platformCb = document.getElementById('keep-warm-platform-allowed');
    // Tenant switch: only for a user of a tenant (KEEPWARM-TENANT-HIDE, owner
    // owner 11-25). A platform-scope admin with no tenant_id never sees this row
    // and no tenant GET/PATCH fires; the help line under the platform row says
    // why. The platform allowance row below is unaffected.
    const tenantHelp = document.getElementById('keep-warm-tenant-help');
    if (tenantRow && tenantCb && _kwHasTenant()) {
        try {
            const data = await apiCall(`/platform/settings/worker.warm_pool_enabled${_kwTenantParam()}`);
            tenantCb.checked = !!(data && data.value);
            tenantRow.classList.remove('hidden');
            tenantHelp?.classList.add('hidden');
        } catch (err) {
            tenantRow.classList.add('hidden'); // key not registered yet -> hide, like KP.7
        }
    } else {
        tenantRow?.classList.add('hidden');
        if (tenantHelp) {
            tenantHelp.classList.remove('hidden');
            tenantHelp.innerHTML = _wkpEscapeHtml('Sign in as a user of a tenant to change the tenant switch');
        }
    }
    // Platform kill switch: platform scope (no tenant_id), default true.
    if (platformRow && platformCb) {
        try {
            const data = await apiCall('/platform/settings/worker.warm_pool_allowed');
            platformCb.checked = !!(data && data.value);
            platformRow.classList.remove('hidden');
        } catch (err) {
            platformRow.classList.add('hidden');
        }
    }
}

async function _kwOnTenantChange(checked) {
    // PATCH stamps THIS tenant's row (tenant switch is written per tenant).
    // KEEPWARM-TENANT-HIDE: with no tenant_id there is nothing to stamp — the
    // row is hidden and this handler returns without any request (defensive:
    // the hidden checkbox cannot be clicked, but a stray change event must
    // never PATCH at platform scope).
    if (!_kwHasTenant()) return;
    try {
        await apiCall(`/platform/settings/worker.warm_pool_enabled${_kwTenantParam()}`, {
            method: 'PATCH',
            body: JSON.stringify({ value: checked }),
        });
        _kwSetStatus(checked
            ? 'Tenant keep-warm switch saved: on for this tenant.'
            : 'Tenant keep-warm switch saved: off for this tenant.');
    } catch (err) {
        _kwSetError(err?.detail?.reason || err?.detail || err?.message || 'Failed to save tenant keep-warm switch.');
        throw err; // caller reverts the checkbox
    }
}

async function _kwOnPlatformChange(checked) {
    // Platform scope: NO tenant_id — this is the kill switch for every tenant.
    try {
        await apiCall('/platform/settings/worker.warm_pool_allowed', {
            method: 'PATCH',
            body: JSON.stringify({ value: checked }),
        });
        _kwSetStatus(checked
            ? 'Platform allowance saved: warm containers allowed (opt-in still applies).'
            : 'Platform kill switch saved: no warm containers for any tenant.');
    } catch (err) {
        _kwSetError(err?.detail?.reason || err?.detail || err?.message || 'Failed to save platform allowance.');
        throw err; // caller reverts the checkbox
    }
}

async function _initKeepWarmSubTab() {
    const tabBtn = document.getElementById('ai-tool-tab-keep-warm');
    const panel = document.getElementById('ai-tool-keep-warm');
    const isPlatformAdmin = !!(currentUser && currentUser.is_platform_admin);
    // Defensive second gate (the AI Config tab above is already platform-admin-
    // only); the backend 403s any non-admin write either way.
    if (!isPlatformAdmin) {
        if (tabBtn) tabBtn.classList.add('hidden');
        if (panel) panel.classList.add('hidden');
        return;
    }
    if (tabBtn) tabBtn.classList.remove('hidden');

    if (_keepWarmWired) {
        _kwLoadRows().catch(err => console.error('keep-warm reload:', err));
        return;
    }
    _keepWarmWired = true;

    document.getElementById('keep-warm-tenant-enabled')?.addEventListener(
        'change', (e) => {
            _kwOnTenantChange(e.target.checked).catch(() => { e.target.checked = !e.target.checked; });
        });
    document.getElementById('keep-warm-platform-allowed')?.addEventListener(
        'change', (e) => {
            _kwOnPlatformChange(e.target.checked).catch(() => { e.target.checked = !e.target.checked; });
        });
    _kwLoadRows().catch(err => console.error('keep-warm init:', err));
}

const _RM_DEFAULTS = {
    gpu_seconds_per_credit_threshold: 60,
    cpu_core_seconds_per_credit_threshold: 600,
    ram_gb_seconds_per_credit_threshold: 300,
    io_mb_per_credit_threshold: 1000,
    enabled: true,
};

async function _initResourceMonitorSubTab() {
    // 2400UI15 — the sub-tab is a PAID-only surface. The gate is FAIL-CLOSED:
    // an unknown/errored entitlements block reads as not-paid, so the tab stays
    // hidden. The router and the three scheduler jobs still SHIP in CE (open-
    // core/manifest.yaml lists them in api_allow), so this hide is UI-only.
    const _paidEdition = await isPaidEdition();
    if (!_paidEdition) {
        const hiddenBtn = document.getElementById('ai-tool-tab-resource-monitor');
        if (hiddenBtn) hiddenBtn.classList.add('hidden');
        return;
    }
    const tabBtn = document.getElementById('ai-tool-tab-resource-monitor');
    if (tabBtn) tabBtn.classList.remove('hidden');

    if (_resourceMonitorSubTabWired) {
        _refreshResourceMonitorSubTab().catch(err => console.error('resource monitor refresh failed:', err));
        return;
    }
    _resourceMonitorSubTabWired = true;

    const els = _rmEls();
    if (!els.save) return;

    els.save.addEventListener('click', _rmSave);
    els.reset.addEventListener('click', () => {
        _rmWrite(_RM_DEFAULTS);
        _rmSetStatus('Defaults loaded. Click Save to commit.');
    });

    await _refreshResourceMonitorSubTab();
}

function _rmEls() {
    return {
        gpu: document.getElementById('rm-gpu-threshold'),
        cpu: document.getElementById('rm-cpu-threshold'),
        ram: document.getElementById('rm-ram-threshold'),
        io:  document.getElementById('rm-io-threshold'),
        minCredits: document.getElementById('rm-min-credits'),
        enabled: document.getElementById('rm-enabled'),
        save: document.getElementById('rm-settings-save'),
        reset: document.getElementById('rm-settings-reset'),
        lastSaved: document.getElementById('rm-settings-last-saved'),
        error: document.getElementById('rm-settings-error'),
        status: document.getElementById('rm-settings-status'),
    };
}

function _rmRead() {
    const els = _rmEls();
    return {
        gpu_seconds_per_credit_threshold: parseFloat(els.gpu.value),
        cpu_core_seconds_per_credit_threshold: parseFloat(els.cpu.value),
        ram_gb_seconds_per_credit_threshold: parseFloat(els.ram.value),
        io_mb_per_credit_threshold: parseFloat(els.io.value),
        enabled: !!els.enabled.checked,
    };
}

function _rmWrite(values) {
    const els = _rmEls();
    if (values.gpu_seconds_per_credit_threshold != null) els.gpu.value = values.gpu_seconds_per_credit_threshold;
    if (values.cpu_core_seconds_per_credit_threshold != null) els.cpu.value = values.cpu_core_seconds_per_credit_threshold;
    if (values.ram_gb_seconds_per_credit_threshold != null) els.ram.value = values.ram_gb_seconds_per_credit_threshold;
    if (values.io_mb_per_credit_threshold != null) els.io.value = values.io_mb_per_credit_threshold;
    if (values.enabled != null) els.enabled.checked = !!values.enabled;
}

function _rmSetError(msg) {
    const els = _rmEls();
    if (!els.error) return;
    if (!msg) {
        els.error.classList.add('hidden');
        return;
    }
    els.error.textContent = msg;
    els.error.classList.remove('hidden');
}

function _rmSetStatus(msg) {
    const els = _rmEls();
    if (!els.status) return;
    if (!msg) {
        els.status.classList.add('hidden');
        return;
    }
    els.status.textContent = msg;
    els.status.classList.remove('hidden');
    setTimeout(() => els.status.classList.add('hidden'), 5000);
}

async function _refreshResourceMonitorSubTab() {
    // Uses the shared apiCall (= fetchApi) helper from api.js — it
    // pulls the JWT from sessionStorage('accessToken'), attaches it
    // as a Bearer header, and retries once on 401 after a silent
    // refresh. Hand-rolling fetch here would (and did) miss those
    // affordances: the Dashboard stores the token under
    // sessionStorage.accessToken, not localStorage.access_token.
    _rmSetError('');
    try {
        const data = await apiCall('/system/resource-monitor/settings');
        _rmWrite(data);
        const els = _rmEls();
        if (els.lastSaved && data.updated_at) {
            try {
                els.lastSaved.textContent = `Last updated ${new Date(data.updated_at).toLocaleString()}`;
            } catch (e) { /* ignore */ }
        }
    } catch (err) {
        const msg = err.message || String(err);
        if (msg.includes('403') || /forbidden/i.test(msg)) {
            _rmSetError('Platform admin only.');
        } else {
            _rmSetError(`Failed to load: ${msg}`);
        }
    }
}

async function _rmSave() {
    _rmSetError('');
    const payload = _rmRead();
    try {
        const saved = await apiCall('/system/resource-monitor/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        _rmWrite(saved);
        _rmSetStatus('Saved.');
    } catch (err) {
        _rmSetError(`Save failed: ${err.message || err}`);
    }
}




// =========================================================================
// mod-assistant-agent §9 — Agent sub-tab
//
// Tenant-admin (parent_user_id IS NULL) or platform-admin only. Reads
// the per-tenant AssistantTenantConfig via GET /assistant/config and
// the aggregate usage via GET /assistant/usage. PUT /assistant/config
// on save; the backend audit-logs every save.
//
// Action allowlist UX: render a checkbox per action in the vocabulary.
// Persist only deviations from "all allowed" — a fully-checked form
// PUTs an empty {} which the backend interprets as "all allowed".
// =========================================================================

// Fallback only — the shipped list is window.ModAssistantActions.actionVocabulary()
// (js/shared/assistant-actions.js), which filters through the canonical
// KNOWN_ACTION_TYPES in js/vendor/assistant-nav.js. Used ONLY when the shared
// runtime is unavailable (pre-§6.4 image / vendor 404).
const _AGENT_ACTION_VOCABULARY_FALLBACK = [
    'navigate',
    'open-new-tab',
    'open-dialog',
    'open-tab',
    'scroll-to',
    'highlight',
    'chain',
];

let _agentSubTabWired = false;

// Save/reload race guard. loadAIConfig() re-runs _initAgentSubTab on every
// reload — mode-switch restore (line ~3547), master Apply, repeated tab
// clicks — and once wired, that fires _refreshAgentSubTab(). A GET that lands
// while the user's save is in flight (or that resolves just after the PUT)
// used to stomp #agent-config-form with a stale server snapshot. So:
//   _agentSaveInFlight — set while the PUT is out; concurrent refreshes skip
//                        the form write (usage still refreshes).
//   _agentRenderSeq    — bumped when the save starts AND right before its own
//                        render, so any refresh that started before the PUT
//                        completed is stale and dropped. Only the PUT
//                        response's render is authoritative.
let _agentSaveInFlight = false;
let _agentRenderSeq = 0;

async function _initAgentSubTab() {
    // FIX-CE-SHOW-ASSISTANT (owner 11-10, "Show them in CE"): the Agent sub-tab
    // is shown in every edition. GET/PUT /assistant/config (routers/assistant.py)
    // ships in CE via open-core/manifest.yaml api_allow + deny_exceptions
    // (FIX-MOD-ASSISTANT-IN-CE-MANIFEST), so the former CE hide (2400UI15 /
    // FIX-B57 reveal-pass marker) is gone. Failed reads degrade inside
    // _refreshAgentSubTab (error notice), never a blank tab.
    // Visible to every tenant user — the backend still gates write
    // (PUT /assistant/config) to tenant-root-admin (parent_user_id IS
    // NULL) or platform admin. Non-admins see the form read-only so
    // they know what the assistant is configured to do but can't
    // change it.
    const isPlatformAdmin = !!(currentUser && currentUser.is_platform_admin);
    const isTenantAdmin = !!(currentUser && currentUser.parent_user_id === null);
    const canEdit = isPlatformAdmin || isTenantAdmin;

    // Tab button is unconditionally visible in the HTML now; no
    // hidden class to flip here.

    if (_agentSubTabWired) {
        _refreshAgentSubTab().catch(err => console.error('agent refresh failed:', err));
        return;
    }
    _agentSubTabWired = true;

    // Lock the form for non-admins so they can read but not edit.
    if (!canEdit) {
        const form = document.getElementById('agent-config-form');
        if (form) {
            form.querySelectorAll('input, textarea, button[type=submit]').forEach((el) => {
                el.disabled = true;
            });
        }
    }

    // Render the action checkboxes once; subsequent refreshes only
    // toggle their .checked state.
    const list = document.getElementById('agent-actions-list');
    if (list && !list.dataset.populated) {
        list.dataset.populated = '1';
        const _sharedActions = window.ModAssistantActions;
        const _vocab = (_sharedActions && _sharedActions.available())
            ? _sharedActions.actionVocabulary()
            : _AGENT_ACTION_VOCABULARY_FALLBACK;
        if (!_vocab.length) {
            console.warn('[assistant] shared action runtime unavailable; using fallback vocabulary');
        }
        _vocab.forEach((action) => {
            const wrap = document.createElement('label');
            wrap.className = 'flex items-center gap-2';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.dataset.action = action;
            cb.checked = true;
            const span = document.createElement('span');
            span.textContent = action;
            wrap.appendChild(cb);
            wrap.appendChild(span);
            list.appendChild(wrap);
        });
    }

    const form = document.getElementById('agent-config-form');
    if (form) {
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            _saveAgentConfig().catch(err => {
                _agentSetStatus(`Save failed: ${err.message || err}`, true);
            });
        });
    }

    _refreshAgentSubTab().catch(err => console.error('agent refresh failed:', err));
}

function _agentSetStatus(msg, isError) {
    const el = document.getElementById('agent-config-status');
    if (!el) return;
    el.textContent = msg || '';
    el.classList.toggle('text-red-600', !!isError);
}

function _agentAuthHeaders(extra) {
    // Match the rest of settings.js — token from sessionStorage,
    // Bearer header built per-call. There is no shared
    // authenticatedFetch helper in this file.
    const token = sessionStorage.getItem('accessToken');
    return Object.assign({}, extra || {}, token ? { 'Authorization': `Bearer ${token}` } : {});
}

async function _refreshAgentSubTab() {
    const apiBase = window.APP_CONFIG?.API_URL || apiUrl || '';
    const headers = _agentAuthHeaders();
    // Capture the render generation now: if a save starts while this GET is
    // in flight, the saved response is the only one allowed to render.
    const seq = _agentRenderSeq;
    // Load both config and usage in parallel.
    const [cfgResp, usageResp] = await Promise.all([
        fetch(`${apiBase}/assistant/config`, { headers }),
        fetch(`${apiBase}/assistant/usage?days=7`, { headers }),
    ]);

    if (cfgResp.ok) {
        const cfg = await cfgResp.json();
        // Drop the form write if a save started (or finished and re-rendered)
        // while this request was out — writing would revert the user's edit.
        if (seq === _agentRenderSeq && !_agentSaveInFlight) {
            _agentWriteConfigForm(cfg);
        }
    } else {
        _agentSetStatus(`Couldn't load config (HTTP ${cfgResp.status})`, true);
    }

    if (usageResp.ok) {
        _agentWriteUsage(await usageResp.json());
    } else {
        // Usage is informational; failing it shouldn't block the form.
        const sessionsEl = document.getElementById('agent-usage-sessions');
        const messagesEl = document.getElementById('agent-usage-messages');
        if (sessionsEl) sessionsEl.textContent = '—';
        if (messagesEl) messagesEl.textContent = '—';
    }
}

function _agentWriteConfigForm(cfg) {
    const enabled = document.getElementById('agent-enabled');
    if (enabled) enabled.checked = !!cfg.enabled;

    const model = document.getElementById('agent-model-id');
    if (model) model.value = cfg.model_id || '';

    const sysPrompt = document.getElementById('agent-system-prompt');
    if (sysPrompt) sysPrompt.value = cfg.system_prompt_override || '';

    const logsCtx = document.getElementById('agent-logs-context');
    if (logsCtx) logsCtx.checked = !!cfg.logs_context_enabled;

    const retention = document.getElementById('agent-retention-hours');
    if (retention) retention.value = (cfg.session_retention_hours || 1);

    // Action allowlist: missing keys are allowed; only explicitly
    // false keys are unchecked.
    const allowlist = (cfg && cfg.action_allowlist) || {};
    document.querySelectorAll('#agent-actions-list input[type=checkbox]').forEach((cb) => {
        const k = cb.dataset.action;
        cb.checked = !(k in allowlist) || allowlist[k] !== false;
    });
}

function _agentReadConfigForm() {
    const enabled = document.getElementById('agent-enabled');
    const model = document.getElementById('agent-model-id');
    const sysPrompt = document.getElementById('agent-system-prompt');
    const logsCtx = document.getElementById('agent-logs-context');
    const retention = document.getElementById('agent-retention-hours');

    // Build allowlist: only persist the deviations (unchecked = false).
    const allowlist = {};
    document.querySelectorAll('#agent-actions-list input[type=checkbox]').forEach((cb) => {
        if (!cb.checked) allowlist[cb.dataset.action] = false;
    });

    return {
        enabled: !!(enabled && enabled.checked),
        model_id: model ? (model.value.trim() || null) : null,
        system_prompt_override: sysPrompt ? (sysPrompt.value || null) : null,
        action_allowlist: allowlist,
        logs_context_enabled: !!(logsCtx && logsCtx.checked),
        session_retention_hours: retention ? Math.max(1, Math.min(168, parseInt(retention.value, 10) || 1)) : 1,
    };
}

async function _saveAgentConfig() {
    _agentSetStatus('Saving…');
    _agentSaveInFlight = true;
    _agentRenderSeq++;  // any refresh that started before this PUT is now stale
    const payload = _agentReadConfigForm();
    try {
        const apiBase = window.APP_CONFIG?.API_URL || apiUrl || '';
        const resp = await fetch(`${apiBase}/assistant/config`, {
            method: 'PUT',
            headers: _agentAuthHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify(payload),
        });
        if (!resp.ok) {
            const raw = await resp.text();
            let detail = raw;
            try {
                const d = JSON.parse(raw).detail;
                if (d && typeof d === 'object') detail = d.message || JSON.stringify(d);
                else if (d) detail = d;
            } catch (_) { /* not JSON — keep raw text */ }
            _agentSetStatus(`Save failed (HTTP ${resp.status}): ${String(detail).slice(0, 200)}`, true);
            return;
        }
        const saved = await resp.json();
        // Re-bump so a refresh that resolved while the PUT was out (its seq
        // was pre-bump) still loses to this render; then paint the server's
        // authoritative copy and clear the flag in finally.
        _agentRenderSeq++;
        _agentWriteConfigForm(saved);
        _agentSetStatus('Saved.');
    } finally {
        _agentSaveInFlight = false;
    }
}

function _agentWriteUsage(usage) {
    const sessionsEl = document.getElementById('agent-usage-sessions');
    const messagesEl = document.getElementById('agent-usage-messages');
    if (sessionsEl) sessionsEl.textContent = String(usage.total_sessions || 0);
    if (messagesEl) messagesEl.textContent = String(usage.total_messages || 0);

    const byModel = document.getElementById('agent-usage-by-model');
    if (byModel) {
        if (!usage.by_model || usage.by_model.length === 0) {
            byModel.textContent = '';
        } else {
            const lines = usage.by_model.map((row) => {
                const id = row.model_id || 'unknown';
                const p = row.prompt_tokens || 0;
                const c = row.completion_tokens || 0;
                return `${id}: ${row.messages} msgs, ${p} prompt + ${c} completion tokens`;
            });
            byModel.textContent = lines.join('  ·  ');
        }
    }
}

// =========================================================================
// GE.MAA.2 (mod-assistant-agent §9.6/9.7) — platform-admin assistant
// defaults, nested inside the Agent sub-tab (#platform-assistant-config).
// Platform-admin only (the backend 403s anyone else; this just hides the
// block so a non-admin never sees a form they can't submit). Reads/writes
// GET/PUT /platform/assistant/config — separate from, and layered above,
// the per-tenant /assistant/config the rest of the Agent sub-tab manages.
// =========================================================================

let _platformAssistantConfigWired = false;

async function _initPlatformAssistantConfig() {
    const isPlatformAdmin = !!(currentUser && currentUser.is_platform_admin);
    const block = document.getElementById('platform-assistant-config');
    // FIX-CE-SHOW-ASSISTANT-DEFAULTS-BLOCK (owner 11-10 10:55 PM CT, EXPLICIT
    // paid-to-CE approval by Tim): the CE edition gate is REMOVED — the block now
    // renders in CE as well as paid. The route behind it ships in CE unchanged
    // (open-core/manifest.yaml api_allow :797 + deny_exceptions :2515 routers/assistant.py,
    // :2521 services/assistant_platform_config_service.py, :2522 services/assistant_router_service.py
    // -> CORE/API/main.py:2527 mounts ("assistant","platform_router","")), so no
    // manifest change was needed and none is proposed. The platform-admin ROLE
    // check below is kept (the backend 403s anyone else; hiding the form from
    // non-admins is unchanged). Paid behaviour is identical: the same admin check,
    // the same hide-then-GET-unhide-on-ok sequence, the same fail-open wiring.
    // The block is still NEVER unhidden here: only a successful GET unhides it
    // (_refreshPlatformAssistantConfig), so a failed read never paints a dead
    // control — it shows the existing error line and returns, no throw.
    if (block) block.classList.add('hidden');
    if (!isPlatformAdmin) return;

    if (_platformAssistantConfigWired) {
        _refreshPlatformAssistantConfig().catch(err => console.error('platform assistant config refresh failed:', err));
        return;
    }
    _platformAssistantConfigWired = true;

    const form = document.getElementById('platform-assistant-config-form');
    if (form) {
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            _savePlatformAssistantConfig().catch(err => {
                _platformAssistantSetStatus(`Save failed: ${err.message || err}`, true);
            });
        });
    }

    _refreshPlatformAssistantConfig().catch(err => console.error('platform assistant config refresh failed:', err));
}

function _platformAssistantSetStatus(msg, isError) {
    const el = document.getElementById('platform-assistant-config-status');
    if (!el) return;
    el.textContent = msg || '';
    el.classList.toggle('text-destructive', !!isError);
    el.classList.toggle('text-success', !isError && !!msg);
}

async function _refreshPlatformAssistantConfig() {
    const apiBase = window.APP_CONFIG?.API_URL || apiUrl || '';
    const headers = _agentAuthHeaders();
    const resp = await fetch(`${apiBase}/platform/assistant/config`, { headers });
    if (!resp.ok) {
        // FIX-CE-SHOW-ASSISTANT-DEFAULTS-BLOCK: the route SHIPS in CE now, so a
        // non-ok GET here is a real failure (5xx / offline / a future deny), not the
        // old expected 404. Same posture in both editions: keep the block hidden
        // (a dead, permanently-unchecked checkbox is worse than no panel) and show
        // the existing error line — no throw, the caller's .catch() also logs it.
        document.getElementById('platform-assistant-config')?.classList.add('hidden');
        _platformAssistantSetStatus(`Couldn't load platform defaults (HTTP ${resp.status})`, true);
        return;
    }
    document.getElementById('platform-assistant-config')?.classList.remove('hidden');
    _platformAssistantWriteConfigForm(await resp.json());
}

function _platformAssistantWriteConfigForm(cfg) {
    const enabled = document.getElementById('platform-assistant-enabled');
    if (enabled) enabled.checked = !!cfg.enabled;

    const model = document.getElementById('platform-assistant-model-id');
    if (model) model.value = cfg.model_id || '';

    const rateMax = document.getElementById('platform-assistant-rate-max');
    if (rateMax) rateMax.value = (cfg.chat_rate_limit_max === null || cfg.chat_rate_limit_max === undefined) ? '' : cfg.chat_rate_limit_max;

    const rateWindow = document.getElementById('platform-assistant-rate-window');
    if (rateWindow) rateWindow.value = (cfg.chat_rate_limit_window_s === null || cfg.chat_rate_limit_window_s === undefined) ? '' : cfg.chat_rate_limit_window_s;

    const effective = document.getElementById('platform-assistant-rate-effective');
    if (effective) {
        effective.textContent = `${cfg.effective_chat_rate_limit_max ?? '—'} messages / ${cfg.effective_chat_rate_limit_window_s ?? '—'}s`;
    }
}

function _platformAssistantReadConfigForm() {
    const enabled = document.getElementById('platform-assistant-enabled');
    const model = document.getElementById('platform-assistant-model-id');
    const rateMax = document.getElementById('platform-assistant-rate-max');
    const rateWindow = document.getElementById('platform-assistant-rate-window');

    const payload = { enabled: !!(enabled && enabled.checked) };
    payload.model_id = model ? (model.value.trim() || '') : '';
    // Always include the rate-limit keys (never omit them): the backend
    // treats an OMITTED key as "don't touch" (exclude_unset) but an
    // explicit `null` as "clear the override back to the env default"
    // (see routers/assistant.py's _PlatformConfigUpdate /
    // assistant_platform_config_service.update_config). A blank field
    // must send `null`, not be left out of the body, or clearing an
    // existing override back to blank is silently a no-op and the stale
    // value keeps getting echoed back by the next GET.
    if (rateMax) {
        payload.chat_rate_limit_max = rateMax.value.trim() === ''
            ? null
            : Math.max(1, Math.min(10000, parseInt(rateMax.value, 10) || 1));
    }
    if (rateWindow) {
        payload.chat_rate_limit_window_s = rateWindow.value.trim() === ''
            ? null
            : Math.max(10, Math.min(86400, parseInt(rateWindow.value, 10) || 10));
    }
    return payload;
}

async function _savePlatformAssistantConfig() {
    _platformAssistantSetStatus('Saving…');
    const payload = _platformAssistantReadConfigForm();
    const apiBase = window.APP_CONFIG?.API_URL || apiUrl || '';
    const resp = await fetch(`${apiBase}/platform/assistant/config`, {
        method: 'PUT',
        headers: _agentAuthHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(payload),
    });
    if (!resp.ok) {
        const raw = await resp.text();
        let detail = raw;
        try {
            const d = JSON.parse(raw).detail;
            if (d && typeof d === 'object') detail = d.message || JSON.stringify(d);
            else if (d) detail = d;
        } catch (_) { /* not JSON — keep raw text */ }
        _platformAssistantSetStatus(`Save failed (HTTP ${resp.status}): ${String(detail).slice(0, 200)}`, true);
        return;
    }
    const saved = await resp.json();
    _platformAssistantWriteConfigForm(saved);
    _platformAssistantSetStatus('Saved.');
}

// ========================================
// Stack TLS (tenant-admin) — GET/PUT /persistent-stacks/tenant-tls.
// Lets a tenant admin choose how publicly-exposed persistent stacks get their
// HTTPS certificate: Let's Encrypt (automatic) or bring-your-own PEM. The
// private key is NEVER rendered back (the API never returns it) and NEVER
// logged or echoed anywhere. Reuses _masterFetch, which surfaces the server's
// `detail` message on 403/422 (and network errors) as thrown Error.message.
// ========================================

function _toggleStackTlsByoFields() {
    const byo = document.getElementById('stack-tls-mode-byo');
    const fields = document.getElementById('stack-tls-byo-fields');
    if (!byo || !fields) return;
    fields.classList.toggle('hidden', !byo.checked);
}

function _setStackTlsMessage(text, isError) {
    const el = document.getElementById('stack-tls-message');
    if (!el) return;
    el.textContent = text || '';
    el.classList.remove('text-red-600', 'text-green-600');
    if (!text) return;
    el.classList.add(isError ? 'text-red-600' : 'text-green-600');
}

// Reflects the stored certificate: a read-only status line when a byo cert is
// on file, plus placeholders that invite replacement. Textareas are always
// left empty — we never populate them from the server.
function _renderStackTlsCertStatus(cfg) {
    const status = document.getElementById('stack-tls-cert-status');
    const cert = document.getElementById('stack-tls-cert');
    const key = document.getElementById('stack-tls-key');
    if (cfg && cfg.has_cert && cfg.mode === 'byo') {
        let line = `Certificate on file: ${cfg.cert_cn || 'unknown'}`;
        if (cfg.cert_not_after) {
            const d = new Date(cfg.cert_not_after);
            const when = isNaN(d.getTime()) ? cfg.cert_not_after : d.toLocaleDateString();
            line += `, expires ${when}`;
        }
        if (status) {
            status.textContent = line;
            status.classList.remove('hidden');
        }
        if (cert) cert.placeholder = 'Paste a new certificate to replace the current one';
        if (key) key.placeholder = 'Paste a new private key to replace the current one';
    } else {
        if (status) {
            status.textContent = '';
            status.classList.add('hidden');
        }
        if (cert) cert.placeholder = '-----BEGIN CERTIFICATE-----';
        if (key) key.placeholder = '-----BEGIN PRIVATE KEY-----';
    }
}

async function loadStackTls() {
    _setStackTlsMessage('', false);
    let cfg;
    try {
        cfg = await _masterFetch('/stacks/tenant-tls', { method: 'GET' });
    } catch (err) {
        _setStackTlsMessage((err && err.message) || 'Failed to load TLS settings', true);
        return;
    }
    const mode = cfg && cfg.mode === 'byo' ? 'byo' : 'letsencrypt';
    const le = document.getElementById('stack-tls-mode-letsencrypt');
    const byo = document.getElementById('stack-tls-mode-byo');
    if (le) le.checked = mode === 'letsencrypt';
    if (byo) byo.checked = mode === 'byo';
    // Never render the stored cert/key back into the textareas.
    const cert = document.getElementById('stack-tls-cert');
    const key = document.getElementById('stack-tls-key');
    if (cert) cert.value = '';
    if (key) key.value = '';
    _renderStackTlsCertStatus(cfg);
    _toggleStackTlsByoFields();
}

async function handleSaveStackTls() {
    const btn = document.getElementById('stack-tls-save');
    const byo = document.getElementById('stack-tls-mode-byo');
    const mode = byo && byo.checked ? 'byo' : 'letsencrypt';
    const body = { mode };
    if (mode === 'byo') {
        const certEl = document.getElementById('stack-tls-cert');
        const keyEl = document.getElementById('stack-tls-key');
        const certPem = certEl ? certEl.value.trim() : '';
        const keyPem = keyEl ? keyEl.value.trim() : '';
        // Always send the pasted PEMs when present. Omitting empty fields lets
        // the user re-confirm byo against an already-stored cert; the server
        // enforces "cert_pem + key_pem required unless a cert is already on
        // file" and returns 422 with a detail message when they are missing.
        if (certPem) body.cert_pem = certPem;
        if (keyPem) body.key_pem = keyPem;
    }

    const originalLabel = btn ? btn.textContent : 'Save';
    if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
    _setStackTlsMessage('', false);
    try {
        const saved = await _masterFetch('/stacks/tenant-tls', {
            method: 'PUT',
            body: JSON.stringify(body),
        });
        const certEl = document.getElementById('stack-tls-cert');
        const keyEl = document.getElementById('stack-tls-key');
        if (certEl) certEl.value = '';
        if (keyEl) keyEl.value = '';
        _renderStackTlsCertStatus(saved);
        _toggleStackTlsByoFields();
        _setStackTlsMessage('TLS policy saved', false);
    } catch (err) {
        _setStackTlsMessage((err && err.message) || 'Failed to save TLS settings', true);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = originalLabel || 'Save'; }
    }
}

// ========================================
// Tenant BYOK tab (GSEC FX.163 p3b3b)
// ========================================

// The BYOK page stays a standalone child (pages/tenant-byok.html + js/tenant-byok.js,
// built by FX.163 p3b3a) and is folded in here as a TAB, never a sidebar row: we fetch
// its markup verbatim into main-panel-byok on first activation, so the child file remains
// the single source of truth and cannot drift into settings.html. The fill guard lives on
// the panel element (ui.js re-injects settings.html per visit, so a fresh div must be
// re-filled), while _byokLoaded — reset per mount like the other wired flags — keeps
// repeat clicks in the SAME mount from re-initializing.
let _byokLoaded = false;

async function _openByokTab() {
    // 2400OPS (PRODRED-B567A): tenant/byok.py is api_deny on CE (carved route ->
    // GET /tenant/byok 404s and the e2e net-guard flags it), so the loader is
    // fail-closed on the SAME isPaidEdition() guard the tab reveal uses (settings.js
    // :233 Knowledge Base, :219 Storage). On CE: no markup fetch, no tenant BYOK init
    // (which is what calls fetchApi('/tenant/byok')), and the panel stays hidden.
    if (!await isPaidEdition()) {
        document.getElementById('main-panel-byok')?.classList.add('hidden');
        return;
    }
    const panel = document.getElementById('main-panel-byok');
    if (!panel) return;
    if (!panel.dataset.byokFilled) {
        try {
            const response = await fetch('/pages/tenant-byok.html');
            panel.innerHTML = response.ok ? await response.text() : '';
            panel.dataset.byokFilled = '1';
        } catch (err) {
            console.error('Error loading tenant BYOK tab:', err);
            return;
        }
    }
    if (_byokLoaded) return;
    _byokLoaded = true;
    try {
        // CE: tenant-byok page denied (manifest deny) - nothing to initialize
    } catch (err) {
        console.error('Error initializing tenant BYOK tab:', err);
    }
}

import { secrets } from './api.js';
import { loadModal, closeModal, checkHarborStatus } from './ui.js';
import { loadResourcePermissions } from './resource-permissions.js';

let canSeeHarborSecrets = false;
let _isPlatformAdmin = false;

export async function loadSecrets() {
    try {
        // First, check if user can see registry secrets
        canSeeHarborSecrets = await checkHarborSecretVisibility();

        const secretsList = await secrets.get();

        // Auto-create placeholder registry secrets only for users who can see them
        if (canSeeHarborSecrets) {
            await ensureRequiredSecretsExist(secretsList);
        }

        // Reload after creating placeholders
        const updatedSecretsList = await secrets.get();

        // Filter out registry secrets if user shouldn't see them
        const filteredSecrets = canSeeHarborSecrets
            ? updatedSecretsList
            : updatedSecretsList.filter(s => !isHarborSecret(s.name));

        renderSecrets(filteredSecrets);
    } catch (error) {
        console.error("Error loading secrets:", error);
        const secretsTableBody = document.querySelector("#secrets-table tbody");
        if (secretsTableBody) {
            secretsTableBody.innerHTML = `<tr><td colspan="3" class="text-center text-red-500 py-4">Could not load secrets.</td></tr>`;
        }
    }
    // After rendering, attach all event listeners for this page
    attachSecretPageEventListeners();
}

// Check if a secret name is a registry-related secret
function isHarborSecret(secretName) {
    const harborSecretNames = ['harbor_registry', 'harbor_username', 'harbor_password'];
    return harborSecretNames.includes(secretName);
}

// Check if current user should see registry secrets
// Returns true if: platform admin OR plan has allow_custom_registry
async function checkHarborSecretVisibility() {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");

    if (!token) return false;

    try {
        const response = await fetch(`${apiUrl}/harbor/status`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (!response.ok) {
            console.log('Harbor status check failed, hiding Harbor secrets');
            return false;
        }

        const harborStatus = await response.json();

        // User can see registry secrets if they're a platform admin OR custom registry is allowed
        _isPlatformAdmin = !!harborStatus.is_platform_admin;
        const canSee = harborStatus.is_platform_admin || harborStatus.can_use_custom_registry;

        return canSee;
    } catch (error) {
        console.error('Error checking Harbor secret visibility:', error);
        return false;
    }
}

// Registry secrets - only for platform admins (tenant 1)
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
    },
    // δ Phase 0 — GitHub token gates the wizard's describe-and-build
    // source type. Without it the search API rate-limits at 60/h
    // shared across the deployment IP, which is unusable in prod.
    // Auto-created as a placeholder for platform admins so they
    // know to drop in a fine-grained PAT (read-only public repo
    // access is sufficient).
    {
        name: 'github_token',
        description: '🔧 CONFIGURE ME: GitHub personal access token (read-only, public repos only). Required for the wizard\'s describe-and-build source type.',
        placeholder: 'PLEASE_CONFIGURE'
    }
];

async function ensureRequiredSecretsExist(existingSecrets) {
    // This function is only called if canSeeHarborSecrets is true
    // So we just need to create placeholders for any missing registry secrets
    const existingNames = new Set(existingSecrets.map(s => s.name));

    for (const requiredSecret of HARBOR_SECRETS) {
        if (!existingNames.has(requiredSecret.name)) {
            try {
                console.log(`Creating placeholder secret: ${requiredSecret.name}`);
                await secrets.create({
                    name: requiredSecret.name,
                    value: requiredSecret.placeholder,
                    description: requiredSecret.description
                });
            } catch (error) {
                console.error(`Failed to create placeholder secret ${requiredSecret.name}:`, error);
                // Continue with other secrets even if one fails
            }
        }
    }
}

function renderSecrets(secretsList) {
    const secretsTableBody = document.querySelector("#secrets-table tbody");
    if (!secretsTableBody) return;

    secretsTableBody.innerHTML = ''; // Clear existing rows
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

    sortedSecrets.forEach(secret => {
        const needsConfig = secret.description?.includes('CONFIGURE ME');
        const statusBadge = needsConfig
            ? '<span class="inline-flex items-center px-2 py-1 rounded text-xs font-medium bg-yellow-200 text-yellow-800">⚠ Not Configured</span>'
            : '<span class="inline-flex items-center px-2 py-1 rounded text-xs font-medium bg-green-200 text-green-800">✓ Configured</span>';

        const shareBtn = (_isPlatformAdmin && !needsConfig)
            ? `<button class="share-secret-button text-purple-600 hover:text-purple-800 font-medium" data-id="${secret.id}" data-name="${secret.name}">Share</button>`
            : '';

        // Strip the `🔧 CONFIGURE ME: ` sentinel prefix from the
        // description — status badge already covers the
        // not-configured signal; the prefix made the row unreadable.
        const cleanDescription = (secret.description || 'No description')
            .replace(/^\s*🔧?\s*CONFIGURE ME:\s*/i, '');

        const row = document.createElement('tr');
        row.className = `cursor-pointer hover:bg-primary/15 transition-colors`;
        row.innerHTML = `
            <td class="border-t px-4 py-3 font-mono text-sm">${secret.name}</td>
            <td class="border-t px-4 py-3 text-gray-600 text-sm">${cleanDescription}</td>
            <td class="border-t px-4 py-3 text-center">${statusBadge}</td>
            <td class="border-t px-4 py-3 text-right space-x-2">
                ${needsConfig ? `<button class="edit-secret-button text-primary hover:text-primary/80 font-medium" data-id="${secret.id}" data-name="${secret.name}">Configure</button>` : ''}
                ${shareBtn}
                <button class="delete-secret-button text-red-600 hover:text-red-800" data-id="${secret.id}">Delete</button>
            </td>
        `;
        row.addEventListener('click', (e) => {
            if (e.target.tagName === 'BUTTON') return;
            document.querySelectorAll('#secrets-table tbody tr').forEach(r => r.classList.remove('bg-primary/20'));
            row.classList.add('bg-primary/20');
            loadResourcePermissions('secret-permissions-panel', 'secret', secret.id, secret.name);
        });
        secretsTableBody.appendChild(row);
    });
}

// --- NEW FUNCTION ---
// This function attaches all necessary event listeners for the secrets page.
function attachSecretPageEventListeners() {
    // Listener for the main "Add Secret" button on the page
    const addSecretButton = document.getElementById('add-secret-button');
    if (addSecretButton) {
        addSecretButton.addEventListener('click', openAddSecretModal);
    }

    // Listeners for any delete buttons in the table
    document.querySelectorAll('.delete-secret-button').forEach(button => {
        button.addEventListener('click', handleDeleteSecret);
    });

    // Listeners for "Configure" buttons (edit placeholders)
    document.querySelectorAll('.edit-secret-button').forEach(button => {
        button.addEventListener('click', handleEditSecret);
    });

    // Listeners for "Share" buttons (platform admin only)
    document.querySelectorAll('.share-secret-button').forEach(button => {
        button.addEventListener('click', handleShareSecret);
    });
}


// --- NEW FUNCTION ---
// This function handles loading the modal and setting up its internal event listeners.
async function openAddSecretModal() {
    const modal = await loadModal('add-secret');
    if (modal) {
        const form = modal.querySelector('#add-secret-form');
        const cancelButton = modal.querySelector('#cancel-secret-button');
        const toggleButton = modal.querySelector('#toggle-secret-visibility');
        const secretInput = modal.querySelector('#secret-value');
        const eyeIcon = modal.querySelector('#eye-icon');
        const eyeOffIcon = modal.querySelector('#eye-off-icon');

        // Password visibility toggle
        if (toggleButton && secretInput && eyeIcon && eyeOffIcon) {
            console.log("Password toggle button found, attaching listener");
            toggleButton.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                console.log("Toggle button clicked, current type:", secretInput.type);
                if (secretInput.type === 'password') {
                    secretInput.type = 'text';
                    eyeIcon.classList.add('hidden');
                    eyeOffIcon.classList.remove('hidden');
                    console.log("Switched to text mode");
                } else {
                    secretInput.type = 'password';
                    eyeIcon.classList.remove('hidden');
                    eyeOffIcon.classList.add('hidden');
                    console.log("Switched to password mode");
                }
            };
        } else {
            console.error("Toggle elements not found:", {toggleButton, secretInput, eyeIcon, eyeOffIcon});
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
        'harbor_password': 'Harbor password for container registry access'
    };
    return descriptions[secretName] || 'Custom secret';
}

async function handleDeleteSecret(event) {
    const secretId = event.target.getAttribute('data-id');
    const secretRow = event.target.closest('tr');
    const secretName = secretRow?.querySelector('td:first-child')?.textContent;

    if (await showConfirm('Are you sure you want to delete this secret? This action cannot be undone.')) {
        try {
            await secrets.delete(secretId);
            loadSecrets();
        } catch (error) {
            console.error("Error deleting secret:", error);
            showError("Failed to delete the secret.");
        }
    }
}


async function handleShareSecret(event) {
    const secretId = event.target.dataset.id;
    const secretName = event.target.dataset.name;

    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");

    let tenants = [];
    try {
        const resp = await fetch(`${apiUrl}/tenants`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (resp.ok) tenants = await resp.json();
    } catch (e) {
        console.error("Failed to load tenants:", e);
    }

    const existing = document.getElementById('share-secret-modal');
    if (existing) existing.remove();

    const tenantCheckboxes = tenants.map(t => `
        <label class="flex items-center space-x-2 py-1">
            <input type="checkbox" class="share-tenant-cb rounded border-gray-300 text-purple-600" value="${t.id}" />
            <span class="text-sm text-gray-700">${t.name || 'Tenant ' + t.id}</span>
        </label>
    `).join('');

    const modalHtml = `
        <div id="share-secret-modal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50 flex items-center justify-center">
            <div class="relative mx-auto p-6 border w-full max-w-md shadow-lg rounded-md bg-white">
                <div class="flex justify-between items-center pb-3 border-b">
                    <h3 class="text-lg font-semibold text-gray-900">Share: ${secretName}</h3>
                    <button onclick="document.getElementById('share-secret-modal').remove()"
                            class="text-gray-400 hover:text-gray-600 text-2xl leading-none" aria-label="Close">&times;</button>
                </div>
                <div class="mt-4 space-y-3">
                    <label class="flex items-center space-x-2 pb-2 border-b">
                        <input type="checkbox" id="share-all-tenants" class="rounded border-gray-300 text-purple-600" />
                        <span class="text-sm font-medium text-gray-800">Share with everyone</span>
                    </label>
                    <div id="tenant-list" class="max-h-48 overflow-y-auto space-y-1">
                        ${tenantCheckboxes || '<p class="text-sm text-gray-500">No tenants found</p>'}
                    </div>
                </div>
                <div class="flex justify-end mt-4 space-x-2">
                    <button onclick="document.getElementById('share-secret-modal').remove()"
                            class="px-4 py-2 bg-gray-200 text-gray-700 text-sm rounded hover:bg-gray-300">Cancel</button>
                    <button id="share-submit-btn"
                            class="px-4 py-2 bg-purple-600 text-white text-sm rounded hover:bg-purple-700">Share</button>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modalHtml);

    document.getElementById('share-all-tenants').addEventListener('change', (e) => {
        const tenantList = document.getElementById('tenant-list');
        tenantList.style.opacity = e.target.checked ? '0.5' : '1';
        tenantList.style.pointerEvents = e.target.checked ? 'none' : 'auto';
    });

    document.getElementById('share-submit-btn').addEventListener('click', async () => {
        const allTenants = document.getElementById('share-all-tenants').checked;
        const selectedIds = [...document.querySelectorAll('.share-tenant-cb:checked')].map(cb => parseInt(cb.value));

        if (!allTenants && selectedIds.length === 0) {
            showInfo('Select at least one tenant or check "Share with everyone"');
            return;
        }

        const btn = document.getElementById('share-submit-btn');
        btn.disabled = true;
        btn.textContent = 'Sharing...';

        try {
            const resp = await fetch(`${apiUrl}/secrets/${secretId}/share`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(allTenants ? { all_tenants: true } : { tenant_ids: selectedIds }),
            });

            if (!resp.ok) {
                const err = await resp.json();
                throw new Error(err.detail || 'Failed to share secret');
            }

            document.getElementById('share-secret-modal').remove();
            showInfo(`Secret "${secretName}" shared successfully`);
        } catch (err) {
            showInfo(err.message);
            btn.disabled = false;
            btn.textContent = 'Share';
        }
    });

    const escHandler = (e) => {
        if (e.key === 'Escape') {
            document.getElementById('share-secret-modal')?.remove();
            document.removeEventListener('keydown', escHandler);
        }
    };
    document.addEventListener('keydown', escHandler);
}


// permission-templates.js
// Permission Templates UI — relocated from the Users page onto the Permissions
// page (Templates tab). This is the same implementation that ran on the Users
// page (raw fetch, form reset, grid error display); showInfo/showError/
// showConfirm are exposed globally on window by ui.js.

// Shared module state
let allTemplates = [];
let allFolders = [];
let allUsers = [];
let currentTemplateId = null;

const apiUrl = () => window.APP_CONFIG?.API_URL || 'http://api.localhost';
const authHeaders = () => ({
    'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
    'Content-Type': 'application/json',
});

// Entry point — called once per page mount when the Templates tab is first
// opened. Attaches listeners and loads the grid.
export function initializePermissionTemplatesPage() {
    initializePermissionTemplatesListeners();
    loadTemplates();
}

// Reload just the grid (listeners already attached this page mount).
export function refreshTemplatesGrid() {
    loadTemplates();
}

function initializePermissionTemplatesListeners() {
    document.getElementById('btn-create-template')?.addEventListener('click', openCreateTemplateModal);
    document.getElementById('close-template-modal')?.addEventListener('click', closeTemplateModal);
    document.getElementById('cancel-template')?.addEventListener('click', closeTemplateModal);
    document.getElementById('save-template')?.addEventListener('click', saveTemplate);
    document.getElementById('close-apply-modal')?.addEventListener('click', closeApplyModal);
    document.getElementById('cancel-apply')?.addEventListener('click', closeApplyModal);
    document.getElementById('confirm-apply')?.addEventListener('click', confirmApplyTemplate);
}

function openCreateTemplateModal() {
    // Reset form
    document.getElementById('template-name').value = '';
    document.getElementById('template-description').value = '';
    document.getElementById('template-level').value = 'viewer';
    document.getElementById('template-duration').value = '';
    document.getElementById('template-can-upload').checked = false;
    document.getElementById('template-can-create-subfolders').checked = false;
    document.getElementById('template-can-move-files').checked = false;
    document.getElementById('template-can-process').checked = false;
    document.getElementById('template-can-share').checked = false;
    document.getElementById('template-can-delete').checked = false;

    document.getElementById('modal-title').textContent = 'Create Template';
    document.getElementById('template-modal').classList.remove('hidden');
}

function closeTemplateModal() {
    document.getElementById('template-modal').classList.add('hidden');
}

function closeApplyModal() {
    document.getElementById('apply-modal').classList.add('hidden');
}

async function loadTemplates() {
    try {
        const response = await fetch(`${apiUrl()}/permissions/templates`, { headers: authHeaders() });
        if (!response.ok) {
            throw new Error(`Failed to load templates: ${response.status}`);
        }
        allTemplates = await response.json();
        renderTemplates();
    } catch (error) {
        console.error('Error loading templates:', error);
        const grid = document.getElementById('templates-grid');
        if (grid) {
            grid.innerHTML = `<div class="col-span-3 text-center py-12"><p class="text-red-500">Error loading templates: ${escapeHtml(error.message)}</p></div>`;
        }
    }
}

function renderTemplates() {
    const grid = document.getElementById('templates-grid');
    if (!grid) return;

    if (allTemplates.length === 0) {
        grid.innerHTML = '<div class="col-span-3 text-center py-12"><p class="text-gray-500">No templates yet. Create your first template!</p></div>';
        return;
    }

    grid.innerHTML = '';
    allTemplates.forEach(template => {
        const card = document.createElement('div');
        card.className = 'bg-white rounded-lg shadow-sm p-6 hover:shadow-md transition-shadow border';

        const capabilities = [];
        if (template.can_upload) capabilities.push('Upload');
        if (template.can_create_subfolders) capabilities.push('Create Subfolders');
        if (template.can_move_files) capabilities.push('Move');
        if (template.can_process) capabilities.push('Process');
        if (template.can_share) capabilities.push('Share');
        if (template.can_delete) capabilities.push('Delete');

        card.innerHTML = `
            <div class="flex justify-between items-start mb-4">
                <h3 class="text-lg font-semibold text-gray-900">${template.name}</h3>
                <span class="px-2 py-1 rounded-full text-xs font-medium bg-primary/20 text-primary">
                    ${template.permission_level}
                </span>
            </div>
            <p class="text-sm text-gray-600 mb-4">${template.description || 'No description'}</p>
            <div class="space-y-2 mb-4">
                <div class="flex items-center text-sm">
                    <svg class="w-4 h-4 mr-2 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                    </svg>
                    <span class="text-gray-600">${template.duration_days ? template.duration_days + ' days' : 'No expiration'}</span>
                </div>
                <div class="flex items-start text-sm">
                    <svg class="w-4 h-4 mr-2 mt-0.5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                    </svg>
                    <span class="text-gray-600">${capabilities.length > 0 ? capabilities.join(', ') : 'Default capabilities'}</span>
                </div>
                <div class="flex items-center text-sm">
                    <svg class="w-4 h-4 mr-2 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"></path>
                    </svg>
                    <span class="text-gray-600">Used ${template.times_used || 0} times</span>
                </div>
            </div>
            <div class="flex gap-2">
                <button data-template-id="${template.id}" class="apply-template-btn flex-1 bg-primary hover:bg-primary/90 text-white px-4 py-2 rounded-lg text-sm">
                    Apply
                </button>
                <button data-template-id="${template.id}" class="delete-template-btn bg-red-100 hover:bg-red-200 text-red-600 px-4 py-2 rounded-lg text-sm">
                    Delete
                </button>
            </div>
        `;

        card.querySelector('.apply-template-btn').addEventListener('click', () => openApplyTemplateModal(template.id));
        card.querySelector('.delete-template-btn').addEventListener('click', () => deleteTemplate(template.id));
        grid.appendChild(card);
    });
}

async function saveTemplate() {
    const name = document.getElementById('template-name').value;
    const description = document.getElementById('template-description').value;
    const level = document.getElementById('template-level').value;
    const duration = parseInt(document.getElementById('template-duration').value) || null;

    if (!name) {
        showInfo('Please enter a template name');
        return;
    }

    try {
        const response = await fetch(`${apiUrl()}/permissions/templates`, {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({
                name,
                description,
                permission_level: level,
                duration_days: duration,
                can_upload: document.getElementById('template-can-upload').checked || null,
                can_create_subfolders: document.getElementById('template-can-create-subfolders').checked || null,
                can_move_files: document.getElementById('template-can-move-files').checked || null,
                can_process: document.getElementById('template-can-process').checked || null,
                can_share: document.getElementById('template-can-share').checked || null,
                can_delete: document.getElementById('template-can-delete').checked || null,
            }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || `Failed to create template: ${response.status}`);
        }

        showInfo('✅ Template created successfully!');
        closeTemplateModal();
        loadTemplates();
    } catch (error) {
        showError('Error creating template: ' + error.message);
    }
}

async function openApplyTemplateModal(templateId) {
    currentTemplateId = templateId;
    const template = allTemplates.find(t => t.id === templateId);
    if (!template) return;

    document.getElementById('apply-template-info').innerHTML = `
        <div class="text-sm">
            <div class="font-semibold text-primary">Applying: ${template.name}</div>
            <div class="text-primary mt-1">${template.permission_level} • ${template.duration_days ? template.duration_days + ' days' : 'Permanent'}</div>
        </div>
    `;

    try {
        const [foldersResponse, usersResponse] = await Promise.all([
            fetch(`${apiUrl()}/assets`, { headers: authHeaders() }),
            fetch(`${apiUrl()}/users`, { headers: authHeaders() }),
        ]);

        if (!foldersResponse.ok || !usersResponse.ok) {
            throw new Error('Failed to load folders or users');
        }

        const folders = await foldersResponse.json();
        const users = await usersResponse.json();

        allFolders = folders;
        allUsers = users;

        const foldersSelect = document.getElementById('apply-folders');
        const usersSelect = document.getElementById('apply-users');

        foldersSelect.innerHTML = '';
        usersSelect.innerHTML = '';

        folders.forEach(folder => {
            const option = document.createElement('option');
            option.value = folder.id;
            option.textContent = folder.name;
            foldersSelect.appendChild(option);
        });

        users.forEach(user => {
            const option = document.createElement('option');
            option.value = user.id;
            option.textContent = `${user.email} (${user.first_name || ''} ${user.last_name || ''})`.trim();
            usersSelect.appendChild(option);
        });

        document.getElementById('apply-override-duration').value = '';
        document.getElementById('apply-modal').classList.remove('hidden');
    } catch (error) {
        showError('Error loading data: ' + error.message);
    }
}

async function confirmApplyTemplate() {
    const folderIds = Array.from(document.getElementById('apply-folders').selectedOptions).map(o => parseInt(o.value));
    const userIds = Array.from(document.getElementById('apply-users').selectedOptions).map(o => parseInt(o.value));
    const overrideDuration = parseInt(document.getElementById('apply-override-duration').value) || null;

    if (folderIds.length === 0 || userIds.length === 0) {
        showInfo('Please select at least one folder and one user');
        return;
    }

    try {
        const response = await fetch(`${apiUrl()}/permissions/templates/${currentTemplateId}/apply`, {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({
                template_id: currentTemplateId,
                cog_ids: folderIds,
                user_ids: userIds,
                override_duration_days: overrideDuration,
            }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || `Failed to apply template: ${response.status}`);
        }

        const result = await response.json();
        showInfo(`✅ Applied template: ${result.successful} successful, ${result.failed} failed`);
        closeApplyModal();
        loadTemplates();
    } catch (error) {
        showError('Error applying template: ' + error.message);
    }
}

async function deleteTemplate(templateId) {
    if (!await showConfirm('Are you sure you want to delete this template?')) {
        return;
    }

    try {
        const response = await fetch(`${apiUrl()}/permissions/templates/${templateId}`, {
            method: 'DELETE',
            headers: authHeaders(),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || `Failed to delete template: ${response.status}`);
        }

        showInfo('✅ Template deleted successfully!');
        loadTemplates();
    } catch (error) {
        showError('Error deleting template: ' + error.message);
    }
}

/**
 * Escape HTML to prevent XSS in innerHTML.
 * Pattern: .replace(/[&<>"']/g, ...)
 */
function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (m) {
        switch (m) {
            case '&': return '&amp;';
            case '<': return '&lt;';
            case '>': return '&gt;';
            case '"': return '&quot;';
            case "'": return '&#39;';
        }
        return m;
    });
}

// Dashboard/js/share-modal.js
// Reusable share modal for files and folders

import { loadModal, closeModal } from './ui.js';

const apiUrl = () => window.APP_CONFIG?.API_URL || 'http://api.localhost';
const authHeaders = () => ({
    'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
    'Content-Type': 'application/json',
});

export async function openShareModal(cogId, cogName, options = {}) {
    const { onSuccess } = options;

    const modal = await loadModal('share');
    if (!modal) return;

    const title = modal.querySelector('#share-modal-title');
    if (title) title.textContent = `Share: ${cogName}`;

    // Load users + tenants
    let allUsers = [];
    let tenants = [];
    let isPlatformAdmin = false;

    try {
        const meResp = await fetch(`${apiUrl()}/me`, { headers: authHeaders() });
        if (meResp.ok) {
            const me = await meResp.json();
            isPlatformAdmin = me.is_platform_admin;
        }
    } catch (e) {}

    try {
        let resp = await fetch(`${apiUrl()}/resource-permissions/sharable-users`, { headers: authHeaders() });
        if (!resp.ok) resp = await fetch(`${apiUrl()}/users/`, { headers: authHeaders() });
        if (resp.ok) allUsers = await resp.json();
    } catch (e) {}

    if (isPlatformAdmin) {
        try {
            const resp = await fetch(`${apiUrl()}/tenants`, { headers: authHeaders() });
            if (resp.ok) tenants = await resp.json();
        } catch (e) {}
    }

    // Add tenant dropdown before user select for admins
    const userSelectParent = modal.querySelector('#share-user-select')?.parentElement;
    if (isPlatformAdmin && tenants.length > 0 && userSelectParent) {
        const tenantDiv = document.createElement('div');
        tenantDiv.className = 'mb-3';
        tenantDiv.innerHTML = `
            <label class="block text-sm font-medium text-gray-700 mb-1">Tenant</label>
            <select id="share-tenant-select" aria-label="Tenant" class="w-full border border-gray-300 rounded-lg px-3 py-2">
                <option value="">Any tenant</option>
                ${tenants.map(t => `<option value="${t.id}">${t.name || 'Tenant ' + t.id}</option>`).join('')}
            </select>
        `;
        userSelectParent.parentElement.insertBefore(tenantDiv, userSelectParent);

        // `allUsers` only holds the CURRENT tenant's users — /sharable-users is
        // tenant-scoped. When a platform admin picks a DIFFERENT tenant we must
        // fetch that tenant's users on demand; filtering the current-tenant list
        // would always yield zero (the bug: tenant dropdown showed no users).
        const _tenantUserCache = {};
        tenantDiv.querySelector('#share-tenant-select').addEventListener('change', async (e) => {
            const tid = e.target.value;
            if (!tid) { populateShareUsers(modal, allUsers, null); return; }
            let list = allUsers.filter(u => u.tenant_id === parseInt(tid));
            if (list.length === 0) {
                if (_tenantUserCache[tid]) {
                    list = _tenantUserCache[tid];
                } else {
                    try {
                        const resp = await fetch(`${apiUrl()}/users/?tenant_id=${tid}`, { headers: authHeaders() });
                        if (resp.ok) { list = await resp.json(); _tenantUserCache[tid] = list; }
                    } catch (err) { /* leave list empty; dropdown shows the placeholder */ }
                }
            }
            // `list` is already tenant-scoped, so pass null to skip re-filtering.
            populateShareUsers(modal, list, null);
        });
    }

    populateShareUsers(modal, allUsers, null);

    function populateShareUsers(modal, users, tenantId) {
        const select = modal.querySelector('#share-user-select');
        if (!select) return;
        const filtered = tenantId ? users.filter(u => u.tenant_id === parseInt(tenantId)) : users;
        select.innerHTML = '<option value="">Select user or team...</option>';
        filtered.forEach(user => {
            const opt = document.createElement('option');
            opt.value = user.id;
            const name = user.name || `${user.first_name || ''} ${user.last_name || ''}`.trim();
            opt.textContent = `${user.email} (${name})`;
            select.appendChild(opt);
        });
    }

    // Role selection
    let selectedRole = '';
    modal.querySelectorAll('.role-option-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            modal.querySelectorAll('.role-option-btn').forEach(b => {
                b.classList.remove('border-primary', 'bg-primary/10');
                b.classList.add('border-gray-300');
            });
            btn.classList.remove('border-gray-300');
            btn.classList.add('border-primary', 'bg-primary/10');
            selectedRole = btn.dataset.role;
            modal.querySelector('#selected-role').value = selectedRole;
            checkValidity();
        });
    });

    // Expiration toggle
    const expirationToggle = modal.querySelector('#set-expiration-toggle');
    const expirationInput = modal.querySelector('#expiration-date');
    if (expirationToggle && expirationInput) {
        expirationToggle.addEventListener('change', () => {
            if (expirationToggle.checked) {
                expirationInput.classList.remove('hidden');
                expirationInput.disabled = false;
            } else {
                expirationInput.classList.add('hidden');
                expirationInput.disabled = true;
            }
        });
    }

    // Validity check
    const confirmBtn = modal.querySelector('#confirm-share-btn');
    const userSelect = modal.querySelector('#share-user-select');

    function checkValidity() {
        if (confirmBtn) {
            confirmBtn.disabled = !userSelect?.value || !selectedRole;
        }
    }

    userSelect?.addEventListener('change', checkValidity);

    // Close handlers
    modal.querySelector('#close-share-modal')?.addEventListener('click', () => closeModal('share-modal'));
    modal.querySelector('#cancel-share-btn')?.addEventListener('click', () => closeModal('share-modal'));

    // Confirm
    confirmBtn?.addEventListener('click', async () => {
        const userId = parseInt(userSelect.value);
        if (!userId || !selectedRole) return;

        confirmBtn.disabled = true;
        confirmBtn.textContent = 'Granting...';

        try {
            const data = {
                target_type: 'cog',
                target_id: cogId,
                user_id: userId,
                level: selectedRole,
            };

            if (expirationToggle?.checked && expirationInput?.value) {
                data.expires_at = new Date(expirationInput.value).toISOString();
            }

            const resp = await fetch(`${apiUrl()}/permissions/cogs/${cogId}`, {
                method: 'POST',
                headers: authHeaders(),
                body: JSON.stringify(data),
            });

            if (!resp.ok) {
                const err = await resp.json();
                throw new Error(err.detail || 'Failed to grant access');
            }

            closeModal('share-modal');
            // Pass the resolved grant details so callers (e.g. bulk share) can
            // fan out to other items WITHOUT re-reading the now-removed modal DOM.
            if (onSuccess) onSuccess({
                userId,
                level: selectedRole,
                expiresAt: data.expires_at || null,
            });
        } catch (err) {
            showInfo(err.message);
            confirmBtn.disabled = false;
            confirmBtn.textContent = 'Grant Access';
        }
    });
}

export async function loadPermissionsForCog(cogId, includeInherited = false) {
    try {
        const qs = includeInherited ? '?include_inherited=true' : '';
        const resp = await fetch(`${apiUrl()}/permissions/cogs/${cogId}${qs}`, {
            headers: authHeaders(),
        });
        if (!resp.ok) return [];
        return await resp.json();
    } catch (e) {
        console.error('Failed to load permissions:', e);
        return [];
    }
}

export async function revokePermission(permissionId) {
    const resp = await fetch(`${apiUrl()}/permissions/${permissionId}`, {
        method: 'DELETE',
        headers: authHeaders(),
    });
    if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || 'Failed to revoke');
    }
}

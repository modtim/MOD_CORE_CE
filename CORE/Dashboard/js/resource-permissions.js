// Dashboard/js/resource-permissions.js
// Reusable resource permissions panel for any resource type

const apiUrl = () => window.APP_CONFIG?.API_URL || 'http://api.localhost';
const authHeaders = () => ({
    'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
    'Content-Type': 'application/json',
});
const escapeHtml = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

export async function loadResourcePermissions(containerId, resourceType, resourceId, resourceName) {
    const container = document.getElementById(containerId);
    if (!container) return;

    container.innerHTML = `
        <div class="space-y-3">
            <div>
                <h4 class="text-sm font-semibold text-gray-800">Permissions: ${resourceName}</h4>
            </div>
            <div class="rp-list text-sm text-gray-500">Loading...</div>
        </div>
    `;

    // Load permissions
    try {
        const resp = await fetch(`${apiUrl()}/resource-permissions/resource/${resourceType}/${resourceId}`, {
            headers: authHeaders(),
        });
        const listDiv = container.querySelector('.rp-list');

        if (!resp.ok) {
            listDiv.innerHTML = '<p class="text-sm text-gray-400">No permission data available</p>';
            return;
        }

        const perms = await resp.json();

        if (perms.length === 0) {
            listDiv.innerHTML = '<p class="text-sm text-gray-400">No permissions granted yet</p>';
        } else {
            // Group by user, keeping each user's individual permission ids so
            // "Revoke" can remove their whole access in one click (like Data).
            const byUser = {};
            perms.forEach(p => {
                const key = p.user_id || 'unknown';
                if (!byUser[key]) {
                    byUser[key] = {
                        name: p.user_name || p.user_email || `User ${p.user_id}`,
                        email: p.user_email || '',
                        userId: p.user_id,
                        tenantName: p.tenant_name,
                        tenantId: p.tenant_id,
                        caps: [],
                        permIds: [],
                    };
                }
                byUser[key].caps.push(p.capability_display);
                if (p.id != null) byUser[key].permIds.push(p.id);
            });

            listDiv.innerHTML = Object.entries(byUser).map(([uid, data]) => {
                const tenantInfo = data.tenantName
                    ? `<span class="text-xs text-primary ml-1">[${escapeHtml(data.tenantName)} #${data.tenantId}]</span>`
                    : '';
                const idInfo = `<span class="text-xs text-gray-300 ml-1">#${data.userId}</span>`;
                return `
                    <div class="flex items-center justify-between py-2 border-b border-gray-100 last:border-0">
                        <div class="min-w-0">
                            <div class="flex items-center flex-wrap">
                                <span class="text-sm font-medium text-gray-700">${escapeHtml(data.name)}</span>
                                ${idInfo}${tenantInfo}
                            </div>
                            <div class="text-xs text-gray-400 mt-0.5 flex flex-wrap gap-1">${data.caps.map(c => `<span class="inline-block px-1.5 py-0.5 bg-gray-100 text-gray-600 rounded">${escapeHtml(c)}</span>`).join('')}</div>
                        </div>
                        <button class="rp-revoke-btn text-xs text-red-600 hover:text-red-800 hover:underline ml-3 flex-shrink-0"
                                data-permids="${data.permIds.join(',')}" data-username="${escapeHtml(data.name)}">Revoke</button>
                    </div>
                `;
            }).join('');

            // Wire up Revoke buttons — DELETE each of the user's capability grants.
            listDiv.querySelectorAll('.rp-revoke-btn').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const ids = (btn.dataset.permids || '').split(',').filter(Boolean);
                    if (!ids.length) return;
                    if (!window.confirm(`Revoke ${btn.dataset.username}'s access to "${resourceName}"?`)) return;
                    btn.disabled = true;
                    btn.textContent = 'Revoking…';
                    // FIX-REVOKE-SHOW-ERROR (D72): the old handler never looked at
                    // the DELETE's HTTP status, so a 403 (Forbidden via
                    // can_manage_resource_grant) or 404 gave no message and the
                    // panel just reloaded with the row still there ("it said I
                    // could but did nothing"). Check the status, report the API's
                    // `detail` through the Dashboard's themed error popup
                    // (window.showError, exported by ui.js), and leave the row in
                    // place with the button re-enabled so the user can retry.
                    let firstError = null;
                    for (const id of ids) {
                        try {
                            const resp = await fetch(`${apiUrl()}/resource-permissions/${id}`, { method: 'DELETE', headers: authHeaders() });
                            if (!resp.ok) {
                                let detail = '';
                                try { detail = String((await resp.json()).detail || ''); } catch (e) { /* non-JSON body */ }
                                firstError = detail || `HTTP ${resp.status}`;
                                break;
                            }
                        } catch (e) { /* network error; reported below */ }
                        if (firstError) break;
                    }
                    if (firstError) {
                        const msg = `Could not revoke: ${firstError}`;
                        if (window.showError) await window.showError(msg, 'Revoke failed');
                        btn.disabled = false;
                        btn.textContent = 'Revoke';
                        return;
                    }
                    // Reload the panel so it reflects the new state.
                    loadResourcePermissions(containerId, resourceType, resourceId, resourceName);
                });
            });
        }
    } catch (e) {
        const listDiv = container.querySelector('.rp-list');
        if (listDiv) listDiv.innerHTML = '<p class="text-sm text-red-500">Failed to load</p>';
    }

}


// 2400LR-g5b: Bulk Permissions page module. ui.js mounts page HTML via innerHTML
// (scripts there are inert), so this page's logic lives here as a real module with an
// exported initializeBulkPermissionsPage() that ui.js calls after mounting. The body is
// wrapped in that function: index.html loads js/main.js as type="module", so a top-level
// `await` here would be a load-time SyntaxError on every page.

function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export async function initializeBulkPermissionsPage() {
        // API utility function
        async function apiCall(endpoint, method = 'GET', body = null) {
            const API_BASE_URL = (window.APP_CONFIG && window.APP_CONFIG.API_URL) || window.location.origin;
            const token = sessionStorage.getItem("accessToken");

            const options = {
                method: method,
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                }
            };

            if (body) {
                options.body = JSON.stringify(body);
            }

            const response = await fetch(`${API_BASE_URL}${endpoint}`, options);

            if (response.status === 204) return null;

            if (!response.ok) {
                let errorMessage = `API request failed with status ${response.status}`;
                try {
                    const errorData = await response.json();
                    if (errorData.detail) {
                        errorMessage = errorData.detail;
                    }
                } catch (e) {
                    errorMessage = response.statusText;
                }
                throw new Error(errorMessage);
            }

            const contentType = response.headers.get("content-type");
            if (contentType && contentType.includes("application/json")) {
                return response.json();
            }
            return null;
        }

        let currentFolders = [];
        let currentUsers = [];
        let currentPermissions = [];

        // Tab switching
        document.querySelectorAll('.bulk-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                // Update tab styles
                document.querySelectorAll('.bulk-tab').forEach(t => {
                    t.classList.remove('border-primary', 'text-primary');
                    t.classList.add('border-transparent', 'text-gray-500');
                });
                tab.classList.add('border-primary', 'text-primary');
                tab.classList.remove('border-transparent', 'text-gray-500');

                // Show corresponding panel
                const panelId = tab.id.replace('tab-', 'panel-');
                document.querySelectorAll('.bulk-panel').forEach(p => p.classList.add('hidden'));
                document.getElementById(panelId).classList.remove('hidden');
            });
        });

        // Load folders and users
        async function loadFoldersAndUsers() {
            try {
                const folders = await apiCall('/assets');
                const users = await apiCall('/users');

                currentFolders = folders;
                currentUsers = users;

                const grantFoldersSelect = document.getElementById('grant-folders');
                const grantUsersSelect = document.getElementById('grant-users');

                folders.forEach(folder => {
                    const option = document.createElement('option');
                    option.value = folder.id;
                    option.textContent = folder.name;
                    grantFoldersSelect.appendChild(option);
                });

                users.forEach(user => {
                    const option = document.createElement('option');
                    option.value = user.id;
                    option.textContent = `${user.email} (${user.first_name || ''} ${user.last_name || ''})`.trim();
                    grantUsersSelect.appendChild(option);
                });

            } catch (error) {
                console.error('Error loading data:', error);
            }
        }

        // Load permissions for revoke/update/extend
        async function loadPermissions() {
            try {
                // Build the list from per-folder GETs using folders already loaded by loadFoldersAndUsers
                const all = await Promise.all(
                    currentFolders.map(f => apiCall(`/permissions/cogs/${f.id}`))
                );
                currentPermissions = all.flat();

                renderPermissionsList('revoke');
                renderPermissionsList('update');
                renderPermissionsList('extend');

            } catch (error) {
                console.error('Error loading permissions:', error);
            }
        }

        function renderPermissionsList(action) {
            const container = document.getElementById(`${action}-permissions-list`);
            container.innerHTML = '';

            let filteredPermissions = currentPermissions;
            if (action === 'extend') {
                filteredPermissions = currentPermissions.filter(p => p.expires_at);
            }

            if (filteredPermissions.length === 0) {
                container.innerHTML = '<p class="text-gray-500 text-sm">No permissions found</p>';
                return;
            }

            filteredPermissions.forEach(perm => {
                const div = document.createElement('div');
                div.className = 'flex items-center p-2 hover:bg-gray-50 rounded';
                div.innerHTML = `
                    <input type="checkbox" aria-label="Select folder #${esc(perm.target_id)} permission" class="${action}-perm-checkbox" data-perm-id="${esc(perm.id)}" class="mr-3">
                    <div class="flex-1">
                        <div class="text-sm font-medium">Folder #${esc(perm.target_id)} - User #${esc(perm.user_id || perm.user_email || 'Team')}</div>
                        <div class="text-xs text-gray-500">Level: ${esc(perm.level)}${perm.expires_at ? ` • Expires: ${new Date(perm.expires_at).toLocaleDateString()}` : ''}</div>
                    </div>
                `;
                container.appendChild(div);
            });
        }

        // Bulk Grant
        document.getElementById('btn-grant').addEventListener('click', async () => {
            const folderIds = Array.from(document.getElementById('grant-folders').selectedOptions).map(o => parseInt(o.value));
            const userIds = Array.from(document.getElementById('grant-users').selectedOptions).map(o => parseInt(o.value));
            const level = document.getElementById('grant-level').value;
            const expiresIn = parseInt(document.getElementById('grant-expires').value) || null;

            if (folderIds.length === 0 || userIds.length === 0) {
                alert('Please select at least one folder and one user');
                return;
            }

            try {
                const result = await apiCall('/bulk-permissions/grant', 'POST', {
                    cog_ids: folderIds,
                    user_ids: userIds,
                    permission_level: level,
                    expires_in_days: expiresIn,
                });

                showResults('Bulk Grant', result);
            } catch (error) {
                alert('Error: ' + error.message);
            }
        });

        // Bulk Revoke
        document.getElementById('btn-revoke').addEventListener('click', async () => {
            const selected = Array.from(document.querySelectorAll('.revoke-perm-checkbox:checked'))
                .map(cb => parseInt(cb.dataset.permId));

            if (selected.length === 0) {
                alert('Please select at least one permission to revoke');
                return;
            }

            if (!confirm(`Are you sure you want to revoke ${selected.length} permission(s)?`)) {
                return;
            }

            try {
                const result = await apiCall('/bulk-permissions/revoke', 'POST', {
                    permission_ids: selected
                });

                showResults('Bulk Revoke', result);
                loadPermissions(); // Refresh the list
            } catch (error) {
                alert('Error: ' + error.message);
            }
        });

        // Bulk Update
        document.getElementById('btn-update').addEventListener('click', async () => {
            const selected = Array.from(document.querySelectorAll('.update-perm-checkbox:checked'))
                .map(cb => parseInt(cb.dataset.permId));

            if (selected.length === 0) {
                alert('Please select at least one permission to update');
                return;
            }

            const capabilities = {};
            document.querySelectorAll('[data-capability]').forEach(cb => {
                if (cb.checked) {
                    capabilities[cb.dataset.capability] = true;
                }
            });

            if (Object.keys(capabilities).length === 0) {
                alert('Please select at least one capability to update');
                return;
            }

            try {
                const result = await apiCall('/bulk-permissions/update-capabilities', 'POST', {
                    permission_ids: selected,
                    ...capabilities
                });

                showResults('Bulk Update', result);
            } catch (error) {
                alert('Error: ' + error.message);
            }
        });

        // Bulk Extend
        document.getElementById('btn-extend').addEventListener('click', async () => {
            const selected = Array.from(document.querySelectorAll('.extend-perm-checkbox:checked'))
                .map(cb => parseInt(cb.dataset.permId));
            const days = parseInt(document.getElementById('extend-days').value);

            if (selected.length === 0) {
                alert('Please select at least one permission to extend');
                return;
            }

            if (!days || days <= 0) {
                alert('Please enter a valid number of days');
                return;
            }

            try {
                const result = await apiCall('/bulk-permissions/extend-expiration', 'POST', {
                    permission_ids: selected,
                    extend_by_days: days
                });

                showResults('Bulk Extend', result);
                loadPermissions(); // Refresh the list
            } catch (error) {
                alert('Error: ' + error.message);
            }
        });

        function showResults(operation, result) {
            const panel = document.getElementById('results-panel');
            const summary = document.getElementById('results-summary');
            const detail = document.getElementById('results-detail');

            summary.innerHTML = `
                <div class="grid grid-cols-3 gap-4">
                    <div class="text-center p-4 bg-gray-50 rounded-lg">
                        <div class="text-2xl font-bold">${esc(result.total_requested)}</div>
                        <div class="text-sm text-gray-600">Total</div>
                    </div>
                    <div class="text-center p-4 bg-green-50 rounded-lg">
                        <div class="text-2xl font-bold text-green-600">${esc(result.successful)}</div>
                        <div class="text-sm text-gray-600">Successful</div>
                    </div>
                    <div class="text-center p-4 bg-red-50 rounded-lg">
                        <div class="text-2xl font-bold text-red-600">${esc(result.failed)}</div>
                        <div class="text-sm text-gray-600">Failed</div>
                    </div>
                </div>
            `;

            detail.innerHTML = '';
            result.results.forEach(r => {
                const div = document.createElement('div');
                div.className = `p-3 rounded border ${r.status.includes('error') || r.status === 'failed' ? 'bg-red-50 border-red-200' : 'bg-green-50 border-green-200'}`;
                div.innerHTML = `
                    <div class="text-sm font-medium">${esc(r.status)}</div>
                    ${r.error_message ? `<div class="text-xs text-red-600">${esc(r.error_message)}</div>` : ''}
                `;
                detail.appendChild(div);
            });

            panel.classList.remove('hidden');
        }

        // Initialize
        await loadFoldersAndUsers();
        await loadPermissions();
}

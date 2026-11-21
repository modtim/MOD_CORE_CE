// permissions.js
// Unified permissions page with resource tabs

import { fetchApi as apiCall, me } from './api.js';
import { openShareModal } from './share-modal.js';
import { loadResourcePermissions } from './resource-permissions.js';
import { initializePermissionTemplatesPage, refreshTemplatesGrid } from './permission-templates.js';

// Templates tab is lazy-initialised once per page mount (listeners attach to
// freshly-injected DOM). Reset in initializePermissionsPage().
let _templatesInited = false;

let currentFolderId = null;
let allFolders = [];
let allItems = [];
let currentUser = null;

const selectedDataIds = new Set();
const selectedResourceIds = new Map(); // tab -> Set

export async function initializePermissionsPage() {
    console.log('Initializing unified Permissions page');

    // Fresh DOM this mount — templates listeners must re-attach on first visit.
    _templatesInited = false;

    setupTabSwitching();

    // Restore last active tab. 'workers' was removed — fall back to data
    // so a persisted value doesn't activate a non-existent tab.
    let savedTab = sessionStorage.getItem('perm-active-tab') || 'data';
    if (savedTab === 'workers') {
        savedTab = 'data';
        sessionStorage.setItem('perm-active-tab', 'data');
    }
    switchPermTab(savedTab);

    try {
        // Bind the page's listeners BEFORE the first await. The Share buttons
        // are static in permissions.html and are enabled by a row click as
        // soon as one item renders, so a click during `await me.get()` /
        // `await loadFolderTree()` used to hit a button with no listener
        // attached (silent no-op: no sharable-users request, no
        // #bulk-grant-modal). The handlers read their data lazily at click
        // time, so binding first is safe; `data-share-bound` stays the
        // product's own marker and is stamped exactly when the click
        // listener is really bound (setupBulkGrantListeners below).
        setupEventListeners();
        setupBulkGrantListeners();

        // Get current user
        currentUser = await me.get();

        await loadFolderTree();

        // Restore expanded folders and reload their children. Drop
        // persisted entries that reference folders that no longer
        // exist (deleted or moved out of view) so stale state doesn't
        // accumulate in sessionStorage.
        const stale = [];
        for (const folderId of expandedFolders) {
            const stillExists = allItems.some(i => i.id === folderId);
            if (!stillExists) {
                stale.push(folderId);
                continue;
            }
            if (!folderChildrenLoaded.has(folderId)) {
                try {
                    await loadFolderChildren(folderId, 0);
                    folderChildrenLoaded.add(folderId);
                } catch (e) {
                    stale.push(folderId);
                }
            }
        }
        if (stale.length) {
            stale.forEach(id => expandedFolders.delete(id));
            sessionStorage.setItem('perm-expanded-folders', JSON.stringify([...expandedFolders]));
        }
        if (expandedFolders.size > 0) {
            renderFolderTree();
        }

        // Restore selected item
        const savedCogId = sessionStorage.getItem('perm-selected-cog-id');
        if (savedCogId) {
            selectFolder(parseInt(savedCogId));
        }

        // Load pending request count for badge
        try {
            const pending = await apiCall('/permission-requests/pending');
            const badge = document.getElementById('pending-requests-badge');
            if (badge && pending.length > 0) {
                badge.textContent = pending.length;
                badge.classList.remove('hidden');
            }
        } catch (e) { /* ignore */ }
    } catch (error) {
        console.error('Error initializing permissions page:', error);
        const folderTree = document.getElementById('folder-tree');
        if (folderTree) {
            folderTree.innerHTML = `
                <div class="text-center text-red-500 py-4">
                    <p>Error loading folders</p>
                    <p class="text-sm mt-2">${error.message}</p>
                </div>
            `;
        }
    }
}

const PAGE_SIZE = 100;
const _savedExpanded = JSON.parse(sessionStorage.getItem('perm-expanded-folders') || '[]');
const expandedFolders = new Set(_savedExpanded);
const folderChildrenLoaded = new Set();

function isFolder(item) {
    return item.cog_type === 'master_folder' || item.cog_type === 'asset_folder' || item.cog_type === 'workflow_folder';
}

async function loadFolderTree() {
    const folderTree = document.getElementById('folder-tree');
    if (!folderTree) return;

    try {
        const rootItems = await apiCall(`/assets?limit=${PAGE_SIZE}`);

        // Re-loading the tree wipes allItems back to roots only, so the
        // children-loaded set must be cleared too — otherwise the
        // restore-expanded loop in initializePermissionsPage thinks the
        // children are already in allItems and skips refetching them,
        // and the user sees an expanded folder with no children until
        // they hard-refresh.
        folderChildrenLoaded.clear();

        allItems = rootItems;
        allFolders = rootItems.filter(isFolder);

        if (rootItems.length === 0) {
            folderTree.innerHTML = `
                <div class="text-center text-gray-500 py-12">
                    <p>No items found</p>
                </div>
            `;
            return;
        }

        renderFolderTree();
    } catch (error) {
        console.error('Error loading folders:', error);
        folderTree.innerHTML = `
            <div class="text-center text-red-500 py-4">
                <p>Error loading folders</p>
            </div>
        `;
    }
}

function renderFolderTree() {
    const folderTree = document.getElementById('folder-tree');
    if (!folderTree) return;
    folderTree.innerHTML = '';

    const rootFolders = allItems.filter(i => !i.parent_id && isFolder(i)).sort((a, b) => a.name.localeCompare(b.name));
    const rootFiles = allItems.filter(i => !i.parent_id && !isFolder(i)).sort((a, b) => a.name.localeCompare(b.name));

    rootFolders.forEach(f => renderTreeItem(folderTree, f, 0));
    rootFiles.forEach(f => renderTreeItem(folderTree, f, 0));
}

function renderTreeItem(container, item, depth) {
    const el = document.createElement('div');
    el.className = 'folder-item px-2 py-1.5 rounded cursor-pointer hover:bg-gray-100 transition-colors';
    el.dataset.folderId = item.id;
    el.style.paddingLeft = `${8 + depth * 16}px`;

    const folder = isFolder(item);
    const expanded = expandedFolders.has(item.id);

    const arrow = folder
        ? `<span class="tree-arrow mr-1 text-gray-400 text-xs inline-block transition-transform ${expanded ? 'rotate-90' : ''}" style="width:12px">&#9654;</span>`
        : '<span style="width:12px;display:inline-block" class="mr-1"></span>';

    const icon = folder
        ? '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-yellow-600 mr-1.5 flex-shrink-0"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'
        : '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-gray-400 mr-1.5 flex-shrink-0"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';

    const textClass = folder ? 'text-sm font-medium text-gray-700' : 'text-sm text-gray-500';
    const checked = selectedDataIds.has(item.id) ? 'checked' : '';

    el.innerHTML = `<div class="flex items-center"><input aria-label="Select item" type="checkbox" class="data-item-cb rounded border-gray-300 text-primary mr-1.5 flex-shrink-0" data-id="${item.id}" ${checked} />${arrow}${icon}<span class="${textClass} truncate">${item.name}</span></div>`;

    const cb = el.querySelector('.data-item-cb');
    cb.addEventListener('click', (e) => e.stopPropagation());
    cb.addEventListener('change', () => {
        if (cb.checked) selectedDataIds.add(item.id);
        else selectedDataIds.delete(item.id);
        updateDataSelectionCount();
    });

    el.addEventListener('click', (e) => {
        e.stopPropagation();
        if (e.target.type === 'checkbox') return;
        if (folder) {
            toggleFolder(item.id, depth);
        }
        selectFolder(item.id);
    });

    container.appendChild(el);

    if (folder && expanded) {
        const childContainer = document.createElement('div');
        childContainer.id = `children-${item.id}`;
        container.appendChild(childContainer);

        const children = allItems.filter(c => c.parent_id === item.id);
        const childFolders = children.filter(isFolder).sort((a, b) => a.name.localeCompare(b.name));
        const childFiles = children.filter(c => !isFolder(c)).sort((a, b) => a.name.localeCompare(b.name));
        childFolders.forEach(c => renderTreeItem(childContainer, c, depth + 1));
        childFiles.forEach(c => renderTreeItem(childContainer, c, depth + 1));
    }
}

async function toggleFolder(folderId, depth) {
    if (expandedFolders.has(folderId)) {
        expandedFolders.delete(folderId);
    } else {
        expandedFolders.add(folderId);

        if (!folderChildrenLoaded.has(folderId)) {
            await loadFolderChildren(folderId, 0);
            folderChildrenLoaded.add(folderId);
        }
    }
    sessionStorage.setItem('perm-expanded-folders', JSON.stringify([...expandedFolders]));
    renderFolderTree();
}

async function loadFolderChildren(parentId, offset) {
    try {
        const children = await apiCall(`/assets?parent_id=${parentId}&limit=${PAGE_SIZE}&offset=${offset}`);

        children.forEach(child => {
            if (!allItems.find(i => i.id === child.id)) {
                allItems.push(child);
            }
        });

        if (children.length === PAGE_SIZE) {
            const childContainer = document.getElementById(`children-${parentId}`);
            if (childContainer) {
                const loadMore = document.createElement('div');
                loadMore.className = 'px-3 py-1.5 text-xs text-primary hover:text-primary/80 cursor-pointer hover:bg-primary/15 rounded';
                loadMore.style.paddingLeft = `${24 + 16}px`;
                loadMore.textContent = `Load more items...`;
                loadMore.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    loadMore.textContent = 'Loading...';
                    await loadFolderChildren(parentId, offset + PAGE_SIZE);
                    renderFolderTree();
                });
                childContainer.appendChild(loadMore);
            }
        }

        if (children.filter(isFolder).length > 0) {
            allFolders = allItems.filter(isFolder);
        }
    } catch (error) {
        console.error(`Error loading children for folder ${parentId}:`, error);
    }
}

async function selectFolder(folderId) {
    currentFolderId = folderId;
    sessionStorage.setItem('perm-selected-cog-id', folderId);

    // Single-item selection is exclusive with the bulk checkbox set. The Share
    // button prefers checked items, so a leftover/"select all" check would make
    // "share this one file" silently apply to everything checked (the
    // "I changed one file and everything changed" bug). Clear it on row-click.
    if (selectedDataIds.size > 0) {
        selectedDataIds.clear();
        document.querySelectorAll('.data-item-cb').forEach(cb => { cb.checked = false; });
        const selectAll = document.getElementById('select-all-data');
        if (selectAll) selectAll.checked = false;
        updateDataSelectionCount();
    }

    // Update selected state in tree
    document.querySelectorAll('.folder-item').forEach(item => {
        if (parseInt(item.dataset.folderId) === folderId) {
            item.classList.add('bg-primary/20', 'border-l-4', 'border-primary');
        } else {
            item.classList.remove('bg-primary/20', 'border-l-4', 'border-primary');
        }
    });

    // Update header — works for both files and folders
    const item = allItems.find(f => f.id === folderId) || allFolders.find(f => f.id === folderId);
    if (item) {
        document.getElementById('selected-folder-name').textContent = item.name;
        const path = getItemPath(item);
        document.getElementById('selected-folder-path').textContent = path;

        // Folder vs File badge + cascade note, so it's obvious whether a change
        // here flows down to contained items (folder) or is item-specific (file).
        const isFolderItem = isFolder(item);
        const badge = document.getElementById('selected-item-type-badge');
        if (badge) {
            badge.textContent = isFolderItem ? '🗂 Folder' : '📄 File';
            badge.className = `ml-2 px-2 py-0.5 rounded-full text-xs font-medium ${isFolderItem ? 'bg-yellow-100 text-yellow-700' : 'bg-gray-100 text-gray-600'}`;
        }
        const note = document.getElementById('folder-cascade-note');
        if (note) {
            if (isFolderItem) {
                note.textContent = '⚠ Permissions set here apply to this folder and everything inside it.';
                note.classList.remove('hidden');
            } else {
                note.textContent = '';
                note.classList.add('hidden');
            }
        }
    }

    // Enable share button
    const shareBtn = document.getElementById('share-folder-btn');
    if (shareBtn) {
        shareBtn.disabled = false;
    }

    // Show permissions panel
    document.getElementById('no-folder-selected').classList.add('hidden');
    document.getElementById('permissions-content').classList.remove('hidden');

    // Load permissions for this folder
    await loadPermissions(folderId);
}

function getItemPath(item) {
    const path = [item.name];
    let current = item;

    while (current.parent_id) {
        const parent = allItems.find(f => f.id === current.parent_id);
        if (!parent) break;
        path.unshift(parent.name);
        current = parent;
    }

    return path.join(' / ');
}

async function loadPermissions(folderId) {
    const tableBody = document.getElementById('permissions-table-body');
    const emptyState = document.getElementById('permissions-empty');
    const permissionsCount = document.getElementById('permissions-count');

    if (!tableBody) return;

    try {
        // Load permissions
        const showInherited = document.getElementById('show-inherited-toggle')?.checked || false;
        const endpoint = showInherited
            ? `/permissions/cogs/${folderId}?include_inherited=true`
            : `/permissions/cogs/${folderId}`;

        const permissions = await apiCall(endpoint);

        if (permissions.length === 0) {
            tableBody.innerHTML = '';
            emptyState?.classList.remove('hidden');
            if (permissionsCount) permissionsCount.textContent = '0';
            return;
        }

        emptyState?.classList.add('hidden');
        if (permissionsCount) permissionsCount.textContent = permissions.length.toString();

        // Render permissions
        tableBody.innerHTML = permissions.map(perm => {
            const userName = perm.user_name || perm.user_email || `User #${perm.user_id}`;
            const tenantBadge = perm.tenant_name ? `<span class="text-xs text-primary">[${perm.tenant_name}]</span>` : '';
            const source = perm.is_inherited ? 'Inherited' : 'Direct';
            const expires = perm.expires_at
                ? new Date(perm.expires_at).toLocaleDateString()
                : 'Never';

            // Inherited rows are editable too — but changing one creates a
            // per-item OVERRIDE (a direct grant on the currently-selected item)
            // instead of editing the parent folder's grant. That's how you give
            // one file a different permission than its folder without cascading
            // to siblings. Direct rows PATCH their own permission as before.
            const roleCell = perm.is_inherited
                ? `<select class="level-override-select border border-amber-300 bg-amber-50 rounded px-2 py-1 text-sm"
                           data-user-id="${perm.user_id || ''}" data-team-id="${perm.team_id || ''}" data-group-id="${perm.keycloak_group_id || ''}"
                           title="Inherited from a parent folder. Changing this sets a permission for THIS item only (an override) — it won't affect other items in the folder.">
                       <option value="viewer" ${perm.level === 'viewer' ? 'selected' : ''}>👁️ Viewer</option>
                       <option value="editor" ${perm.level === 'editor' ? 'selected' : ''}>✏️ Editor</option>
                       <option value="owner" ${perm.level === 'owner' ? 'selected' : ''}>👑 Owner</option>
                   </select>`
                : `<select aria-label="Access level for user" class="level-select border border-gray-300 rounded px-2 py-1 text-sm" data-perm-id="${perm.id}">
                       <option value="viewer" ${perm.level === 'viewer' ? 'selected' : ''}>👁️ Viewer</option>
                       <option value="editor" ${perm.level === 'editor' ? 'selected' : ''}>✏️ Editor</option>
                       <option value="owner" ${perm.level === 'owner' ? 'selected' : ''}>👑 Owner</option>
                   </select>`;

            return `
                <tr>
                    <td class="px-6 py-4 whitespace-nowrap">
                        <div class="text-sm font-medium text-gray-900">${userName} <span class="text-xs text-gray-400">#${perm.user_id}</span> ${perm.tenant_name ? `<span class="text-xs text-primary">[${perm.tenant_name} #${perm.tenant_id}]</span>` : ''}</div>
                        <div class="text-sm text-gray-500">${perm.user_email || ''}</div>
                    </td>
                    <td class="px-6 py-4 whitespace-nowrap">${roleCell}</td>
                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${source}</td>
                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${expires}</td>
                    <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                        ${!perm.is_inherited ? `
                            <button class="revoke-btn text-red-600 hover:text-red-900" data-perm-id="${perm.id}">Revoke</button>
                        ` : ''}
                    </td>
                </tr>
            `;
        }).join('');

        // Attach level change handlers
        document.querySelectorAll('.level-select').forEach(select => {
            select.addEventListener('change', async () => {
                const permId = select.dataset.permId;
                const newLevel = select.value;
                try {
                    await apiCall(`/permissions/${permId}`, {
                        method: 'PATCH',
                        body: JSON.stringify({ level: newLevel }),
                    });
                    select.style.borderColor = '#22c55e';
                    setTimeout(() => { select.style.borderColor = ''; }, 2000);
                } catch (e) {
                    showError('Failed to update: ' + e.message);
                    await loadPermissions(currentFolderId);
                }
            });
        });

        // Attach override handlers for inherited rows — changing the level
        // creates/updates a DIRECT grant on the current item only (override),
        // leaving the parent folder's grant (and sibling items) untouched.
        document.querySelectorAll('.level-override-select').forEach(select => {
            select.addEventListener('change', async () => {
                if (!currentFolderId) return;
                const body = { target_type: 'cog', target_id: currentFolderId, level: select.value };
                if (select.dataset.userId) body.user_id = parseInt(select.dataset.userId);
                else if (select.dataset.teamId) body.team_id = parseInt(select.dataset.teamId);
                else if (select.dataset.groupId) body.keycloak_group_id = select.dataset.groupId;
                else { showError('Cannot override: unknown grantee'); return; }
                try {
                    await apiCall(`/permissions/cogs/${currentFolderId}`, {
                        method: 'POST',
                        body: JSON.stringify(body),
                    });
                    select.style.borderColor = '#22c55e';
                    await loadPermissions(currentFolderId);
                } catch (e) {
                    showError('Failed to set override: ' + e.message);
                    await loadPermissions(currentFolderId);
                }
            });
        });

        // Attach revoke button handlers
        document.querySelectorAll('.revoke-btn').forEach(btn => {
            btn.addEventListener('click', () => revokePermission(parseInt(btn.dataset.permId)));
        });

    } catch (error) {
        console.error('Error loading permissions:', error);
        tableBody.innerHTML = `
            <tr>
                <td colspan="5" class="px-6 py-4 text-center text-red-500">
                    Error loading permissions: ${error.message}
                </td>
            </tr>
        `;
    }
}

function setupEventListeners() {
    // Share button
    document.getElementById('share-folder-btn')?.addEventListener('click', () => {
        if (selectedDataIds.size > 0) {
            // Bulk share all checked items
            openBulkResourceGrantModal('data', 'cog', [...selectedDataIds]);
        } else if (currentFolderId) {
            const folderName = allItems.find(f => f.id === currentFolderId)?.name || 'Item';
            openShareModal(currentFolderId, folderName, {
                onSuccess: () => loadPermissions(currentFolderId),
            });
        }
    });

    // Share modal - role selection
    document.querySelectorAll('.role-option-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.role-option-btn').forEach(b => {
                b.classList.remove('border-primary', 'bg-primary/10');
                b.classList.add('border-gray-300');
            });
            btn.classList.add('border-primary', 'bg-primary/10');
            btn.classList.remove('border-gray-300');
            document.getElementById('selected-role').value = btn.dataset.role;
            checkShareFormValidity();
        });
    });

    // Share modal - user selection
    document.getElementById('share-user-select')?.addEventListener('change', checkShareFormValidity);

    // Expiration toggle
    document.getElementById('set-expiration-toggle')?.addEventListener('change', (e) => {
        const dateInput = document.getElementById('expiration-date');
        if (e.target.checked) {
            dateInput.classList.remove('hidden');
            dateInput.disabled = false;
        } else {
            dateInput.classList.add('hidden');
            dateInput.disabled = true;
            dateInput.value = '';
        }
    });

    // Show inherited toggle
    document.getElementById('show-inherited-toggle')?.addEventListener('change', () => {
        if (currentFolderId) {
            loadPermissions(currentFolderId);
        }
    });

    // Share modal buttons
    document.getElementById('close-share-modal')?.addEventListener('click', closeShareModal);
    document.getElementById('cancel-share-btn')?.addEventListener('click', closeShareModal);
    document.getElementById('confirm-share-btn')?.addEventListener('click', confirmShare);

    // Access requests button
    document.getElementById('view-requests-btn')?.addEventListener('click', () => {
        document.getElementById('requests-modal').classList.remove('hidden');
        loadAccessRequests();
    });

    // Requests modal
    document.getElementById('close-requests-modal')?.addEventListener('click', () => {
        document.getElementById('requests-modal').classList.add('hidden');
    });
}

async function _legacyOpenShareModal() {
    if (!currentFolderId) return;

    // Load users
    try {
        const users = await apiCall('/users');
        const select = document.getElementById('share-user-select');
        if (select) {
            select.innerHTML = '<option value="">Select user or team...</option>';
            users.forEach(user => {
                const option = document.createElement('option');
                option.value = user.id;
                option.textContent = `${user.email} (${user.first_name || ''} ${user.last_name || ''})`.trim();
                select.appendChild(option);
            });
        }

        // Reset form
        document.querySelectorAll('.role-option-btn').forEach(b => {
            b.classList.remove('border-primary', 'bg-primary/10');
            b.classList.add('border-gray-300');
        });
        document.getElementById('selected-role').value = '';
        document.getElementById('set-expiration-toggle').checked = false;
        document.getElementById('expiration-date').classList.add('hidden');
        document.getElementById('expiration-date').disabled = true;
        document.getElementById('confirm-share-btn').disabled = true;

        document.getElementById('share-modal').classList.remove('hidden');
    } catch (error) {
        console.error('Error loading users:', error);
        showError('Failed to load users');
    }
}

function closeShareModal() {
    document.getElementById('share-modal').classList.add('hidden');
}

function checkShareFormValidity() {
    const userId = document.getElementById('share-user-select').value;
    const role = document.getElementById('selected-role').value;
    const confirmBtn = document.getElementById('confirm-share-btn');

    if (confirmBtn) {
        confirmBtn.disabled = !userId || !role;
    }
}

async function confirmShare() {
    const userId = parseInt(document.getElementById('share-user-select').value);
    const role = document.getElementById('selected-role').value;
    const setExpiration = document.getElementById('set-expiration-toggle').checked;
    const expirationDate = document.getElementById('expiration-date').value;

    if (!userId || !role || !currentFolderId) return;

    try {
        const permissionData = {
            target_type: 'cog',
            target_id: currentFolderId,
            user_id: userId,
            level: role,
        };

        if (setExpiration && expirationDate) {
            permissionData.expires_at = new Date(expirationDate).toISOString();
        }

        await apiCall(`/permissions/cogs/${currentFolderId}`, {
            method: 'POST',
            body: JSON.stringify(permissionData)
        });

        closeShareModal();
        await loadPermissions(currentFolderId);
    } catch (error) {
        console.error('Error sharing folder:', error);
        showError(`Failed to share folder: ${error.message}`);
    }
}

async function revokePermission(permissionId) {
    if (!await showConfirm('Are you sure you want to revoke this permission?')) {
        return;
    }

    try {
        await apiCall(`/permissions/${permissionId}`, { method: 'DELETE' });
        await loadPermissions(currentFolderId);
    } catch (error) {
        console.error('Error revoking permission:', error);
        showError(`Failed to revoke permission: ${error.message}`);
    }
}

async function loadAccessRequests() {
    const pendingList = document.getElementById('pending-requests-list');
    const myRequestsList = document.getElementById('my-requests-list');
    const badge = document.getElementById('pending-requests-badge');

    // Load pending requests (requests for folders I own)
    if (pendingList) {
        try {
            const pending = await apiCall('/permission-requests/pending');
            if (pending.length === 0) {
                pendingList.innerHTML = '<div class="text-gray-500 text-center py-4">No pending requests</div>';
            } else {
                pendingList.innerHTML = pending.map(req => `
                    <div class="border border-gray-200 rounded-lg p-4 mb-3">
                        <div class="flex items-center justify-between mb-2">
                            <div>
                                <span class="font-medium text-gray-800">${req.requester_name}</span>
                                <span class="text-gray-500 text-sm ml-1">(${req.requester_email})</span>
                            </div>
                            <span class="text-xs text-gray-400">${new Date(req.created_at).toLocaleDateString()}</span>
                        </div>
                        <div class="text-sm text-gray-600 mb-2">
                            Requesting <strong>${req.requested_level}</strong> access to <strong>${req.resource_name || req.cog_name}${req.resource_type === 'workflow' ? ' <span class="text-xs text-primary">(workflow)</span>' : ''}</strong>
                            ${req.justification ? `<p class="mt-1 text-gray-500 italic">"${req.justification}"</p>` : ''}
                        </div>
                        <div class="flex space-x-2">
                            <button class="approve-req-btn px-3 py-1 bg-green-600 text-white text-xs rounded hover:bg-green-700" data-id="${req.id}">Approve</button>
                            <button class="deny-req-btn px-3 py-1 bg-red-600 text-white text-xs rounded hover:bg-red-700" data-id="${req.id}">Deny</button>
                        </div>
                    </div>
                `).join('');

                pendingList.querySelectorAll('.approve-req-btn').forEach(btn => {
                    btn.addEventListener('click', () => reviewRequest(btn.dataset.id, 'approve'));
                });
                pendingList.querySelectorAll('.deny-req-btn').forEach(btn => {
                    btn.addEventListener('click', () => reviewRequest(btn.dataset.id, 'deny'));
                });
            }

            if (badge) {
                if (pending.length > 0) {
                    badge.textContent = pending.length;
                    badge.classList.remove('hidden');
                } else {
                    badge.classList.add('hidden');
                }
            }
        } catch (error) {
            console.error('Error loading pending requests:', error);
            pendingList.innerHTML = '<div class="text-red-500 text-center py-4">Failed to load</div>';
        }
    }

    // Load my requests
    if (myRequestsList) {
        try {
            const myReqs = await apiCall('/permission-requests/my-requests');
            if (myReqs.length === 0) {
                myRequestsList.innerHTML = '<div class="text-gray-500 text-center py-4">You haven\'t requested access to anything</div>';
            } else {
                const statusColors = { pending: 'bg-yellow-100 text-yellow-800', approved: 'bg-green-100 text-green-800', denied: 'bg-red-100 text-red-900', cancelled: 'bg-gray-100 text-gray-600' };
                myRequestsList.innerHTML = myReqs.map(req => `
                    <div class="flex items-center justify-between py-2 border-b border-gray-100 last:border-0">
                        <div>
                            <span class="text-sm font-medium text-gray-700">${req.resource_name || req.cog_name}${req.resource_type === 'workflow' ? ' <span class="text-xs text-primary">(workflow)</span>' : ''}</span>
                            <span class="text-xs text-gray-400 ml-2">${req.requested_level}</span>
                        </div>
                        <span class="px-2 py-0.5 text-xs rounded ${statusColors[req.status] || 'bg-gray-100'}">${req.status}</span>
                    </div>
                `).join('');
            }
        } catch (error) {
            console.error('Error loading my requests:', error);
            myRequestsList.innerHTML = '<div class="text-red-500 text-center py-4">Failed to load</div>';
        }
    }
}

async function reviewRequest(requestId, action) {
    const notes = action === 'deny' ? prompt('Reason for denial (optional):') : null;

    try {
        await apiCall(`/permission-requests/${requestId}/review`, {
            method: 'POST',
            body: JSON.stringify({ action, review_notes: notes }),
        });
        loadAccessRequests();
        if (currentFolderId) loadPermissions(currentFolderId);
    } catch (error) {
        console.error('Error reviewing request:', error);
        showError(`Failed to ${action}: ${error.message}`);
    }
}


// =========================================================================
// Tab switching + resource list loading
// =========================================================================

function setupTabSwitching() {
    document.querySelectorAll('.perm-tab').forEach(tab => {
        tab.addEventListener('click', () => switchPermTab(tab.dataset.tab));
    });
}

function switchPermTab(tabName) {
    document.querySelectorAll('.perm-tab').forEach(t => {
        t.classList.remove('border-primary', 'text-primary');
        t.classList.add('border-transparent', 'text-gray-500');
    });
    const activeTab = document.querySelector(`.perm-tab[data-tab="${tabName}"]`);
    if (activeTab) {
        activeTab.classList.remove('border-transparent', 'text-gray-500');
        activeTab.classList.add('border-primary', 'text-primary');
    }

    document.querySelectorAll('.perm-panel').forEach(p => p.classList.add('hidden'));
    const panel = document.getElementById(`perm-panel-${tabName}`);
    if (panel) panel.classList.remove('hidden');

    sessionStorage.setItem('perm-active-tab', tabName);

    if (tabName === 'data') {
        // Data tab loads via existing loadFolderTree
        // Restore selected item
        const savedId = sessionStorage.getItem('perm-selected-cog-id');
        if (savedId && currentFolderId !== parseInt(savedId)) {
            selectFolder(parseInt(savedId));
        }
    } else if (tabName === 'templates') {
        // Relocated Permission Templates UI — attach listeners once per page
        // mount (init also loads the grid); on later visits just refresh it.
        if (!_templatesInited) {
            initializePermissionTemplatesPage();
            _templatesInited = true;
        } else {
            refreshTemplatesGrid();
        }
    } else {
        loadResourceList(tabName);
    }
}

const RESOURCE_TAB_CONFIG = {
    workflows: { endpoint: '/road/?limit=100', type: 'workflow', nameField: 'name', idField: 'id' },
    secrets: { endpoint: '/secrets/?limit=100', type: 'secret', nameField: 'name', idField: 'id' },
    workers: { endpoint: '/workers?limit=100', type: 'worker', nameField: 'worker_name', idField: 'id', fallbackEndpoint: '/workers/available-for-tenant/1' },
    apikeys: { endpoint: '/api-keys/?limit=100', type: 'api_key', nameField: 'key_prefix', idField: 'id' },
};

async function loadResourceList(tabName) {
    const config = RESOURCE_TAB_CONFIG[tabName];
    if (!config) return;

    const listContainer = document.getElementById(`perm-list-${tabName}`);
    if (!listContainer) return;

    listContainer.innerHTML = '<p class="text-sm text-gray-400 py-4 text-center">Loading...</p>';

    try {
        let items = await apiCall(config.endpoint);

        if (!items || items.length === 0) {
            listContainer.innerHTML = `<p class="text-sm text-gray-400 py-8 text-center">No ${tabName} found</p>`;
            return;
        }

        // Handle workers special case (nested response)
        if (tabName === 'workers' && !Array.isArray(items)) {
            const all = [...(items.tenant_owned || []), ...(items.platform_assigned || []), ...(items.platform_pool || [])];
            if (all.length > 0) items = all;
        }

        if (!selectedResourceIds.has(tabName)) selectedResourceIds.set(tabName, new Set());
        const selected = selectedResourceIds.get(tabName);

        listContainer.innerHTML = '';
        items.forEach(item => {
            const name = item[config.nameField] || `${config.type} ${item[config.idField]}`;
            const itemId = item[config.idField];
            const el = document.createElement('div');
            el.className = 'flex items-center px-3 py-2 rounded cursor-pointer hover:bg-gray-100 transition-colors text-sm text-gray-700';
            el.dataset.itemId = itemId;
            el.dataset.itemName = name;

            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.className = 'resource-item-cb rounded border-gray-300 text-primary mr-2 flex-shrink-0';
            cb.dataset.id = itemId;
            cb.dataset.tab = tabName;
            cb.checked = selected.has(itemId);
            cb.addEventListener('click', (e) => e.stopPropagation());
            cb.addEventListener('change', () => {
                if (cb.checked) selected.add(itemId);
                else selected.delete(itemId);
                updateResourceSelectionCount(tabName);
            });

            const label = document.createElement('span');
            label.className = 'truncate';
            label.textContent = name;

            el.appendChild(cb);
            el.appendChild(label);

            el.addEventListener('click', (e) => {
                if (e.target.type === 'checkbox') return;
                listContainer.querySelectorAll('div').forEach(d => d.classList.remove('bg-primary/20'));
                el.classList.add('bg-primary/20');
                loadResourcePermissions(`perm-detail-${tabName}`, config.type, itemId, name);

                const header = document.getElementById(`perm-header-${tabName}`);
                if (header) header.textContent = name;
                const shareBtn = document.querySelector(`.resource-share-btn[data-tab="${tabName}"]`);
                if (shareBtn && !shareBtn.textContent.includes('items')) {
                    shareBtn.disabled = false;
                    shareBtn.textContent = 'Share';
                }
            });

            listContainer.appendChild(el);
        });
    } catch (error) {
        console.error(`Error loading ${tabName}:`, error);
        listContainer.innerHTML = `<p class="text-sm text-red-500 py-4 text-center">Failed to load</p>`;
    }
}


// =========================================================================
// Bulk selection helpers
// =========================================================================

function updateDataSelectionCount() {
    const count = selectedDataIds.size;
    const countEl = document.getElementById('selected-count-data');
    const shareBtn = document.getElementById('share-folder-btn');
    if (countEl) {
        countEl.textContent = `${count} selected`;
        countEl.classList.toggle('hidden', count === 0);
    }
    if (shareBtn) {
        if (count > 0) {
            shareBtn.textContent = `Share ${count} items`;
            shareBtn.disabled = false;
        } else {
            shareBtn.textContent = 'Share';
            shareBtn.disabled = !currentFolderId;
        }
    }
}

function updateResourceSelectionCount(tabName) {
    const selected = selectedResourceIds.get(tabName) || new Set();
    const count = selected.size;
    const countEl = document.getElementById(`selected-count-${tabName}`);
    const shareBtn = document.querySelector(`.resource-share-btn[data-tab="${tabName}"]`);
    if (countEl) {
        countEl.textContent = `${count} selected`;
        countEl.classList.toggle('hidden', count === 0);
    }
    if (shareBtn) {
        if (count > 0) {
            shareBtn.textContent = `Share ${count} items`;
            shareBtn.disabled = false;
        } else {
            shareBtn.textContent = 'Share';
            // Keep enabled if a single item was clicked
        }
    }
}

function setupBulkGrantListeners() {
    // Data tab select-all
    document.getElementById('select-all-data')?.addEventListener('change', (e) => {
        const checked = e.target.checked;
        document.querySelectorAll('.data-item-cb').forEach(cb => {
            cb.checked = checked;
            const id = parseInt(cb.dataset.id);
            if (checked) selectedDataIds.add(id);
            else selectedDataIds.delete(id);
        });
        updateDataSelectionCount();
    });

    // Data tab bulk grant
    document.getElementById('bulk-grant-data')?.addEventListener('click', () => {
        if (selectedDataIds.size === 0) return;
        const ids = [...selectedDataIds];
        openShareModal(ids[0], `${ids.length} items`, {
            onSuccess: async ({ userId, level, expiresAt } = {}) => {
                // The first item was shared via the modal, which resolves the
                // chosen user/role (and hands it back here instead of us having
                // to re-read the now-removed modal DOM). Fan the same grant out
                // to every remaining selected item.
                let granted = 1; // first item already granted by the modal
                let failed = 0;
                for (const id of ids.slice(1)) {
                    try {
                        await apiCall(`/permissions/cogs/${id}`, {
                            method: 'POST',
                            body: JSON.stringify({
                                target_type: 'cog',
                                target_id: id,
                                user_id: userId,
                                level,
                                ...(expiresAt ? { expires_at: expiresAt } : {}),
                            }),
                        });
                        granted++;
                    } catch (e) {
                        failed++;
                    }
                }
                if (failed > 0) {
                    showInfo(`Shared ${granted} of ${ids.length} items (${failed} failed).`);
                } else {
                    showInfo(`Shared ${granted} item${granted > 1 ? 's' : ''}.`);
                }
                selectedDataIds.clear();
                updateDataSelectionCount();
                renderFolderTree();
            },
        });
    });

    // Resource tab select-all
    document.querySelectorAll('.select-all-resource').forEach(cb => {
        cb.addEventListener('change', (e) => {
            const tabName = cb.dataset.tab;
            const checked = e.target.checked;
            if (!selectedResourceIds.has(tabName)) selectedResourceIds.set(tabName, new Set());
            const selected = selectedResourceIds.get(tabName);

            document.querySelectorAll(`.resource-item-cb[data-tab="${tabName}"]`).forEach(itemCb => {
                itemCb.checked = checked;
                const id = parseInt(itemCb.dataset.id) || itemCb.dataset.id;
                if (checked) selected.add(id);
                else selected.delete(id);
            });
            updateResourceSelectionCount(tabName);
        });
    });

    // Resource tab share buttons (handle both single and bulk).
    // initializePermissionsPage() can run again on re-navigation against the
    // same persistent buttons; guard so each button binds exactly one click
    // listener (otherwise a single click opens the modal twice).
    document.querySelectorAll('.resource-share-btn').forEach(btn => {
        if (btn.dataset.shareBound) return;
        btn.dataset.shareBound = '1';
        btn.addEventListener('click', () => {
            const tabName = btn.dataset.tab;
            const config = RESOURCE_TAB_CONFIG[tabName];
            if (!config) return;

            const selected = selectedResourceIds.get(tabName);
            if (selected && selected.size > 0) {
                openBulkResourceGrantModal(tabName, config.type, [...selected]);
            } else {
                // Single item — find the highlighted one. The row's highlight
                // class is `bg-primary/20`; the `/` makes it an INVALID CSS
                // selector, so document.querySelector(...) here threw and the
                // single-item Share silently no-opped. Use classList.contains
                // (a JS string match) instead of a CSS selector.
                const listEl = document.getElementById(`perm-list-${tabName}`);
                const highlighted = listEl
                    ? [...listEl.querySelectorAll('[data-item-id]')].find(d => d.classList.contains('bg-primary/20'))
                    : null;
                if (highlighted) {
                    const itemId = parseInt(highlighted.dataset.itemId) || highlighted.dataset.itemId;
                    const itemName = highlighted.dataset.itemName;
                    openBulkResourceGrantModal(tabName, config.type, [itemId]);
                }
            }
        });
    });
}

async function openBulkResourceGrantModal(tabName, resourceType, itemIds) {
    const existing = document.getElementById('bulk-grant-modal');
    if (existing) existing.remove();

    let allUsers = [];
    let tenants = [];
    const isPlatformAdmin = currentUser?.is_platform_admin;

    try {
        allUsers = await apiCall('/resource-permissions/sharable-users');
    } catch (e) {
        try { allUsers = await apiCall('/users/'); } catch (e2) {}
    }

    if (isPlatformAdmin) {
        try { tenants = await apiCall('/tenants'); } catch (e) {}
    }

    const tenantDropdown = isPlatformAdmin && tenants.length > 0
        ? `<div>
               <label class="block text-sm font-medium text-gray-700 mb-1">Tenant</label>
               <select id="bulk-grant-tenant" aria-label="Tenant" class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm">
                   <option value="">Any tenant</option>
                   ${tenants.map(t => `<option value="${t.id}">${t.name || 'Tenant ' + t.id}</option>`).join('')}
               </select>
           </div>`
        : '';

    // API keys aren't a tiered resource — you can't hand someone the secret
    // (shown once at creation, never returned). The only meaningful shared
    // grant is read/oversight: view + view_usage. So skip the role tiers and
    // treat it as a binary grant.
    const isBinary = resourceType === 'api_key';
    const roleSection = isBinary
        ? `<div class="text-sm text-gray-600 bg-gray-50 border border-gray-200 rounded-md p-3">
               Grants <span class="font-medium">view + usage</span> access to this API key —
               the user can see the key exists and its usage, but cannot use it
               (the secret is only shown once, at creation).
           </div>`
        : `<div>
               <label class="block text-sm font-medium text-gray-700 mb-2">Role</label>
               <div class="grid grid-cols-3 gap-2">
                   <button type="button" class="bulk-role-btn border-2 border-gray-300 rounded-lg p-2 text-center hover:border-primary hover:bg-primary/15" data-role="viewer">
                       <div class="text-sm font-medium">Viewer</div>
                       <div class="text-xs text-gray-400">Read only</div>
                   </button>
                   <button type="button" class="bulk-role-btn border-2 border-gray-300 rounded-lg p-2 text-center hover:border-primary hover:bg-primary/15" data-role="editor">
                       <div class="text-sm font-medium">Editor</div>
                       <div class="text-xs text-gray-400">View + edit + run</div>
                   </button>
                   <button type="button" class="bulk-role-btn border-2 border-gray-300 rounded-lg p-2 text-center hover:border-primary hover:bg-primary/15" data-role="manager">
                       <div class="text-sm font-medium">Manager</div>
                       <div class="text-xs text-gray-400">Edit + share (no delete)</div>
                   </button>
               </div>
           </div>`;

    const modalHtml = `
        <div id="bulk-grant-modal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50 flex items-center justify-center">
            <div class="relative mx-auto p-6 border w-full max-w-md shadow-lg rounded-md bg-white">
                <div class="flex justify-between items-center pb-3 border-b">
                    <h3 class="text-lg font-semibold text-gray-900">Grant Access to ${itemIds.length} item${itemIds.length > 1 ? 's' : ''}</h3>
                    <button onclick="document.getElementById('bulk-grant-modal').remove()"
                            class="text-gray-400 hover:text-gray-600 text-2xl leading-none" aria-label="Close">&times;</button>
                </div>
                <div class="mt-4 space-y-4">
                    ${tenantDropdown}
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1">User</label>
                        <select aria-label="User" id="bulk-grant-user" class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm">
                            <option value="">Select user...</option>
                        </select>
                    </div>
                    ${roleSection}
                    <div id="bulk-grant-progress" class="hidden text-sm text-primary"></div>
                </div>
                <div class="flex justify-end mt-4 space-x-2">
                    <button onclick="document.getElementById('bulk-grant-modal').remove()"
                            class="px-4 py-2 bg-gray-200 text-gray-700 text-sm rounded hover:bg-gray-300">Cancel</button>
                    <button id="bulk-grant-submit"
                            class="px-4 py-2 bg-primary text-white text-sm rounded hover:bg-primary/90">Grant Access</button>
                </div>
            </div>
        </div>
    `;

    // A duplicate click / re-entrant call can insert a second modal while we
    // were awaiting above. Clear any modal right before inserting so exactly
    // one remains, then scope ALL queries below to that node — never
    // document-wide getElementById, which returns the first match and would
    // leave the visible (last-inserted) modal's dropdown empty.
    document.querySelectorAll('#bulk-grant-modal').forEach(m => m.remove());
    document.body.insertAdjacentHTML('beforeend', modalHtml);
    const modal = document.body.querySelector('#bulk-grant-modal');

    // Populate user dropdown (filtered by tenant for admins)
    function populateUsers(tenantId) {
        const select = modal.querySelector('#bulk-grant-user');
        if (!select) return;
        const filtered = tenantId
            ? allUsers.filter(u => u.tenant_id === parseInt(tenantId))
            : allUsers;
        select.innerHTML = '<option value="">Select user...</option>' +
            filtered.map(u => {
                const name = u.name || `${u.first_name || ''} ${u.last_name || ''}`.trim();
                return `<option value="${u.id}">${u.email} (${name})</option>`;
            }).join('');
    }

    populateUsers(null);

    modal.querySelector('#bulk-grant-tenant')?.addEventListener('change', async (e) => {
        const tid = e.target.value;
        // allUsers only holds the current tenant's users (/sharable-users is
        // tenant-scoped). For a platform admin picking another tenant, fetch
        // that tenant's users on demand and merge, else the dropdown is empty.
        if (tid && !allUsers.some(u => u.tenant_id === parseInt(tid))) {
            try {
                const more = await apiCall(`/users/?tenant_id=${tid}`);
                if (Array.isArray(more)) allUsers = allUsers.concat(more);
            } catch (err) { /* leave as-is; dropdown shows placeholder */ }
        }
        populateUsers(tid);
    });

    // Binary resources (api_key) have no tier buttons — the grant is implicit.
    let selectedRole = isBinary ? 'access' : '';
    modal.querySelectorAll('.bulk-role-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            modal.querySelectorAll('.bulk-role-btn').forEach(b => {
                b.classList.remove('border-primary', 'bg-primary/10');
                b.classList.add('border-gray-300');
            });
            btn.classList.remove('border-gray-300');
            btn.classList.add('border-primary', 'bg-primary/10');
            selectedRole = btn.dataset.role;
            checkBulkValidity();
        });
    });

    function checkBulkValidity() {
        const userSelect = modal.querySelector('#bulk-grant-user');
        const submitBtn = modal.querySelector('#bulk-grant-submit');
        if (submitBtn) submitBtn.disabled = !userSelect?.value || !selectedRole;
    }

    modal.querySelector('#bulk-grant-user')?.addEventListener('change', checkBulkValidity);
    checkBulkValidity();

    modal.querySelector('#bulk-grant-submit')?.addEventListener('click', async () => {
        const userId = parseInt(modal.querySelector('#bulk-grant-user').value);
        if (!userId) { showInfo('Select a user'); return; }
        if (!selectedRole) { showInfo('Select a role'); return; }

        const submitBtn = modal.querySelector('#bulk-grant-submit');
        const progress = modal.querySelector('#bulk-grant-progress');
        submitBtn.disabled = true;
        submitBtn.textContent = 'Granting...';
        progress.classList.remove('hidden');

        // Map role to capabilities (no delete — that's destructive and must be granted explicitly)
        const roleCaps = {
            access: ['view', 'view_usage'],
            viewer: ['view', 'view_details', 'view_status', 'view_runs', 'view_versions'],
            editor: ['view', 'view_details', 'edit', 'create', 'execute', 'use', 'view_status', 'view_runs', 'view_versions', 'use_in_workflow'],
            manager: ['view', 'view_details', 'edit', 'create', 'execute', 'use', 'share', 'manage_permissions', 'view_status', 'view_runs', 'view_versions', 'use_in_workflow'],
        };
        const caps = roleCaps[selectedRole] || ['view'];

        // : count outcomes per item instead of
        // swallowing every error. The grant API answers 200 for no-ops:
        //   {"skipped": true}          - capability not applicable to this type
        //   the existing row (id, no permission_id) - grant already present
        // Counting both as granted made the modal claim success for a no-op
        // (DIAG: it said I could but then did not do it). Shared code: paid
        // gets the same honest reporting; no licence gate or cap added.
        let ok = 0, skipped = 0, existing = 0, failed = 0;
        const reasons = [];
        const noteReason = (e) => {
            const d = (e && typeof e.detail === 'string') ? e.detail
                : (e && e.detail && e.detail.message) ? e.detail.message
                : (e && e.message) ? e.message : 'grant failed';
            if (!reasons.includes(d)) reasons.push(d);
        };
        if (resourceType === 'cog') {
            // Cogs use the LEVEL-based permissions system (same as the files
            // page, the single-share modal, and the "who has access" panel).
            // Writing capability grants here left the share invisible and
            // ungranted in the file UI — the reported "only the first file
            // shared" bug (the grants went to a different table the panel
            // never reads).
            const levelMap = { viewer: 'viewer', editor: 'editor', manager: 'editor' };
            const level = levelMap[selectedRole] || 'viewer';
            const overrides = selectedRole === 'manager' ? { can_share: true } : {};
            for (let i = 0; i < itemIds.length; i++) {
                progress.textContent = `Granting ${i + 1}/${itemIds.length}...`;
                try {
                    const res = await apiCall(`/permissions/cogs/${itemIds[i]}`, {
                        method: 'POST',
                        body: JSON.stringify({
                            target_type: 'cog',
                            target_id: itemIds[i],
                            user_id: userId,
                            level,
                            ...overrides,
                        }),
                    });
                    // The cog grant returns the Permission row (id + level); a
                    // row for the same target/level that already exists comes
                    // back the same way, so treat a re-grant as existing only
                    // when the level is unchanged is not knowable client-side:
                    // count it ok (the grant is in effect either way).
                    if (res && (res.skipped === true)) skipped++;
                    else ok++;
                } catch (e) { failed++; noteReason(e); }
            }
        } else {
            // Non-cog resources (workflows, secrets, etc.) use the
            // capability-based resource-permissions system. One item asks for
            // several capabilities: the ITEM counts granted if ANY capability
            // took effect, already-had if only existing rows came back,
            // skipped only if every capability was a no-op, failed if nothing
            // took effect and at least one call errored.
            for (let i = 0; i < itemIds.length; i++) {
                progress.textContent = `Granting ${i + 1}/${itemIds.length}...`;
                let tookEffect = false, hadExisting = false, anyFailed = false;
                for (const capName of caps) {
                    try {
                        const res = await apiCall('/resource-permissions/grant', {
                            method: 'POST',
                            body: JSON.stringify({
                                user_id: userId,
                                resource_type: resourceType,
                                capability_name: capName,
                                resource_id: itemIds[i],
                            }),
                        });
                        // skipped:true  -> capability not applicable to this
                        //                 resource type (no-op, HTTP 200)
                        // no permission_id but an id -> the EXISTING row was
                        //                 returned (already had this access)
                        if (res && res.skipped === true) { /* no-op */ }
                        else if (res && res.permission_id === undefined && res.id !== undefined) hadExisting = true;
                        else tookEffect = true;
                    } catch (e) { anyFailed = true; noteReason(e); }
                }
                if (tookEffect) ok++;
                else if (hadExisting) existing++;
                else if (anyFailed) failed++;
                else skipped++;
            }
        }

        // : tell the user what really happened
        // instead of always claiming a grant. Shared code: paid gets the same
        // honest reporting; no licence gate or cap added.
        const total = itemIds.length;
        if (ok === 0 && existing === 0 && failed > 0) {
            // Nothing took effect and at least one call errored: error toast
            // with the API's detail, keep the modal open for a retry.
            showError(`Grant failed: ${reasons.join('; ') || 'no grant took effect'}`);
            submitBtn.disabled = false;
            submitBtn.textContent = 'Grant Access';
            progress.classList.add('hidden');
            return;
        }
        const parts = [];
        if (ok > 0) parts.push(isBinary
            ? `Granted access on ${ok} API key${ok > 1 ? 's' : ''}`
            : `Granted ${selectedRole} access on ${ok} item${ok > 1 ? 's' : ''}`);
        if (existing > 0) parts.push(`${existing} already had access`);
        if (skipped > 0) parts.push(`${skipped} were skipped (not applicable to this resource type)`);
        if (failed > 0) showError(`${failed} of ${total} failed: ${reasons.join('; ') || 'grant failed'}`);
        if (parts.length) showInfo(parts.join('; '));

        modal.remove();

        // Clear selection
        const selected = selectedResourceIds.get(tabName);
        if (selected) selected.clear();
        updateResourceSelectionCount(tabName);
        loadResourceList(tabName);
    });

    document.addEventListener('keydown', function esc(e) {
        if (e.key === 'Escape') {
            document.getElementById('bulk-grant-modal')?.remove();
            document.removeEventListener('keydown', esc);
        }
    });
}

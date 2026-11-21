// In Dashboard/js/files.js

import * as api from './api.js';
import { loadModal, closeModal } from './ui.js';
import { openPreview, closePreview, initPreviewListeners, loadThumbnail, clearThumbnailCache } from './file-preview.js';
import { openShareModal, loadPermissionsForCog, revokePermission } from './share-modal.js';
import { showContextMenu, hideContextMenu } from './context-menu.js';
import { isNameCollisionError, resolveCollision } from './collision-resolve.js';
import { dataLabelChip } from './data-label-chip.js';
import { runBulkAction, bulkActionFetch, REASON_CODES } from './bulk-aggregator.js';
import { showBulkSkipWarning } from './bulk-skip-modal.js';

// --- Helpers ---
function truncateName(name, maxLen = 10) {
    if (name.length <= maxLen) return name;
    const ext = name.lastIndexOf('.') > 0 ? name.slice(name.lastIndexOf('.')) : '';
    const base = name.slice(0, name.length - ext.length);
    const available = maxLen - ext.length - 1; // 1 for ellipsis
    if (available < 4) return name.slice(0, maxLen - 1) + '…';
    // Show beginning + end of the base name
    const startLen = Math.ceil(available * 0.6);
    const endLen = available - startLen;
    if (endLen > 0) {
        return base.slice(0, startLen) + '…' + base.slice(-endLen) + ext;
    }
    return base.slice(0, available) + '…' + ext;
}

function setViewMode(mode) {
    viewMode = mode;
    localStorage.setItem('fileViewMode', mode);

    // Update toggle button styles
    const gridBtn = document.getElementById('view-grid-btn');
    const listBtn = document.getElementById('view-list-btn');
    if (gridBtn && listBtn) {
        if (mode === 'grid') {
            gridBtn.className = 'p-1.5 rounded-md bg-white shadow-sm text-gray-700';
            listBtn.className = 'p-1.5 rounded-md text-gray-400 hover:text-gray-600';
        } else {
            gridBtn.className = 'p-1.5 rounded-md text-gray-400 hover:text-gray-600';
            listBtn.className = 'p-1.5 rounded-md bg-white shadow-sm text-gray-700';
        }
    }

    // Update grid container classes
    const fileList = document.getElementById('file-list');
    if (fileList) {
        if (mode === 'grid') {
            fileList.className = 'grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-4';
        } else {
            fileList.className = 'flex flex-col gap-1 min-w-0';
        }
    }

    // Clear stale thumbnail cache and re-render
    clearThumbnailCache();
    const currentParent = breadcrumbTrail.length > 0
        ? breadcrumbTrail[breadcrumbTrail.length - 1].id
        : null;
    loadFiles(currentParent);
}

// deleted-files-filter (#4): flip between the live folder view and the
// tenant's soft-deleted files. Updates the toggle button's active styling +
// aria-pressed, hides folder-scoped controls that don't apply to a flat
// deleted list, and reloads.
function setShowDeleted(on) {
    showDeleted = !!on;

    const btn = document.getElementById('toggle-deleted-btn');
    if (btn) {
        btn.setAttribute('aria-pressed', showDeleted ? 'true' : 'false');
        btn.className = 'px-3 py-1.5 rounded-lg transition-colors text-sm font-medium flex items-center ' + (showDeleted
            ? 'bg-primary text-white hover:bg-primary/90'
            : 'border border-gray-300 text-gray-600 hover:bg-gray-50');
        btn.title = showDeleted ? 'Back to files' : 'Show soft-deleted files';
    }
    const label = document.getElementById('toggle-deleted-label');
    if (label) label.textContent = showDeleted ? 'Deleted (on)' : 'Deleted';

    // Folder-scoped toolbar controls don't apply to the flat, tenant-wide
    // deleted list — dim them while it's shown.
    ['upload-file-button', 'make-folder-button', 'up-button'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = showDeleted;
    });

    // Leaving the deleted view: any leftover selection was of deleted rows.
    selectedCogIds.clear();
    selectedItems.clear();
    closePreview();

    loadFiles(null);
}

function formatFileSize(bytes) {
    if (!bytes || bytes === 0) return '--';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function formatDate(dateStr) {
    if (!dateStr) return '--';
    const d = new Date(dateStr);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

// --- Download Job State ---
let activeDownloadJobId = null;
let downloadPollInterval = null;
let isDownloadModalMinimized = false;

// --- State ---
// State is now properly encapsulated within the files module.
let breadcrumbTrail = [];
let selectedCogIds = new Set(); // Changed to Set for multi-select
let selectedItems = new Map(); // Map of cogId -> {id, name, type}
let folderPickerCallback = null; // Callback function when folder is selected
let folderPickerTrail = []; // Breadcrumb trail for folder picker navigation
let selectedFolderDestination = null; // Currently selected destination folder
let searchQuery = ''; // Current search query
// deleted-files-filter (#4): when true, the Files view lists the tenant's
// soft-deleted files (GET /cogs/?deleted=true) with a per-file Restore action
// instead of the live folder contents.
let showDeleted = false;
// Bug-4: monotonic load counter. Rapid search typing/clearing fires many
// loadFiles() calls; only the LATEST may render — otherwise a slow stale
// response lands after a clear and blanks the view.
let _loadSeq = 0;
let viewMode = localStorage.getItem('fileViewMode') || 'grid'; // 'grid' or 'list'
let _previewCogId = null;
let _previewCogName = null;
let _previewOwnerId = null;
let _currentUserId = null;
// Bug-1: "Home" is the tenant's root MASTER_FOLDER — a real, file-holding
// folder — not the no-parent level (which only accepts master folders, so
// files dropped there vanished into an auto-created folder). Resolved once.
let ROOT_FOLDER_ID = null;
async function getRootFolderId() {
    if (ROOT_FOLDER_ID == null) {
        try { ROOT_FOLDER_ID = (await api.cogs.getRoot()).id; }
        catch (e) { console.warn('Could not resolve root folder:', e); }
    }
    return ROOT_FOLDER_ID;
}

// Live refresh: the resolved parent of the current view, so a background poll
// can re-pull this folder. Recipients of a share/revoke see it without an
// explicit page reload (no websockets — gentle polling + refresh-on-focus).
let _currentParentId = null;
let _filesPollTimer = null;
// Signature of the last-rendered view. A background poll that fetches identical
// contents skips the clear+rebuild so the list doesn't flicker every 12s when
// nothing actually changed.
let _lastRenderSig = null;

function refreshCurrentFilesView() {
    // Don't yank the view out from under an in-progress action: skip while a
    // modal is open or the user has items selected (mid multi-select).
    if (document.hidden) return;
    // Modals inject into #modal-container and closeModal() empties it, so a
    // non-empty container means a dialog is open — don't refresh under it.
    const mc = document.getElementById('modal-container');
    if (mc && mc.children.length > 0) return;
    if (selectedCogIds.size > 0) return;
    loadFiles(searchQuery.trim() !== '' ? null : _currentParentId, { poll: true });
    if (_previewCogId) loadPreviewPermissions(_previewCogId);
}

function startFilesPolling() {
    if (_filesPollTimer) return;
    // 12s cadence balances freshness against re-render churn.
    _filesPollTimer = setInterval(refreshCurrentFilesView, 12000);
    // Snap-refresh the moment the tab regains focus.
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refreshCurrentFilesView();
    });
}

// --- Core Logic ---
export async function loadFiles(parentId, opts = {}) {
    const fileListContainer = document.getElementById('file-list');
    if (!fileListContainer) return;

    const _seq = ++_loadSeq;  // bug-4: tag this load; discard if superseded

    if (!_currentUserId) {
        try {
            const me = await api.me.get();
            _currentUserId = me.id;
        } catch (e) { /* ignore */ }
    }

    // Home (no breadcrumb) maps to the tenant root folder, not the no-parent
    // level — so uploads, new folders, and files all live + show here.
    // deleted-files-filter (#4): the Deleted view is tenant-wide and not scoped
    // to a folder, so skip the root-folder resolution there.
    if (parentId == null && searchQuery.trim() === '' && !showDeleted) {
        parentId = await getRootFolderId();
    }

    // Remember the resolved folder so the background poll can re-pull it.
    if (searchQuery.trim() === '') _currentParentId = parentId;

    try {
        let items;

        // If searching, use recursive search endpoint
        if (showDeleted) {
            // deleted-files-filter (#4): tenant-wide soft-deleted files.
            items = await api.cogs.get(null, true, { deleted: true });
        } else if (searchQuery.trim() !== '') {
            items = await api.cogs.search(searchQuery);
        } else {
            // Otherwise, get items for current folder
            items = await api.cogs.get(parentId, true);
        }

        // Bug-4: a newer loadFiles started while we awaited — drop this stale
        // result so it can't overwrite the current view (e.g. a search response
        // landing after the search was cleared).
        if (_seq !== _loadSeq) return;

        // move-to-root-phantom (#5): drop any malformed/empty entries before we
        // count or render. A file moved to root could come back without a
        // usable id (a "phantom" the empty-check counted as present but the row
        // render produced nothing for), leaving a blank list with no "empty"
        // message. Filtering here keeps items.length in sync with what actually
        // renders, so either real rows show or the proper empty state does.
        items = (items || []).filter(it => it && it.id != null);

        // Skip a no-op re-render: on a background POLL, if the folder's visible
        // contents are identical to what we last rendered, don't clear+rebuild
        // the list — that flickered every 12s even when nothing changed.
        // Navigation and explicit reloads (no `poll`) always render.
        const _sig = viewMode + '|' + String(parentId) + '|' + JSON.stringify(
            (items || []).map(i => [
                i.id, i.name, i.cog_type,
                i.size_bytes ?? i.size ?? '',
                i.updated_at ?? i.modified ?? i.updated ?? '',
                i.processing_status ?? i.status ?? '',
                i.thumbnail_path ?? i.preview_path ?? '',
            ])
        );
        if (opts.poll && _sig === _lastRenderSig) return;
        _lastRenderSig = _sig;

        fileListContainer.innerHTML = ''; // Clear previous items

        // Set container layout based on view mode
        if (viewMode === 'list') {
            fileListContainer.className = 'flex flex-col gap-1';
        } else {
            fileListContainer.className = 'grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-4';
        }

        // Sync toggle button styles
        const gridBtn = document.getElementById('view-grid-btn');
        const listBtn = document.getElementById('view-list-btn');
        if (gridBtn && listBtn) {
            if (viewMode === 'grid') {
                gridBtn.className = 'p-1.5 rounded-md bg-white shadow-sm text-gray-700';
                listBtn.className = 'p-1.5 rounded-md text-gray-400 hover:text-gray-600';
            } else {
                gridBtn.className = 'p-1.5 rounded-md text-gray-400 hover:text-gray-600';
                listBtn.className = 'p-1.5 rounded-md bg-white shadow-sm text-gray-700';
            }
        }

        if (items.length === 0) {
            if (showDeleted) {
                fileListContainer.innerHTML = '<p class="text-center text-gray-500 col-span-full">No deleted files.</p>';
            } else if (searchQuery.trim() !== '') {
                fileListContainer.innerHTML = '<p class="text-center text-gray-500 col-span-full">No files or folders match your search.</p>';
            } else {
                fileListContainer.innerHTML = '<p class="text-center text-gray-500 col-span-full">This folder is empty.</p>';
            }
        } else {
            // Add list header when in list mode
            if (viewMode === 'list') {
                const header = document.createElement('div');
                header.className = 'flex items-center px-3 py-2 text-xs font-semibold text-gray-500 uppercase tracking-wider border-b border-gray-200';
                header.innerHTML = `
                    <div class="w-8"></div>
                    <div class="w-6 mr-3"></div>
                    <div class="flex-1">Name</div>
                    <div class="w-24 text-right">Size</div>
                    <div class="w-28 text-right">Modified</div>
                    <div class="w-20 text-center">Type</div>
                `;
                fileListContainer.appendChild(header);
            }

            items.forEach(item => {
                // move-to-root-phantom (#5): a root/moved file may arrive with a
                // null/absent cog_type; treat a missing type as a plain file
                // instead of throwing (which would abort the loop and blank the
                // whole list).
                const isFolder = (item.cog_type || '').includes('folder');
                const isSelected = selectedCogIds.has(item.id);
                const fileElement = document.createElement('div');

                // File icon color
                const folderColors = {
                    'master_folder': 'text-primary',
                    'asset_folder': 'text-primary',
                    'default': 'text-gray-500'
                };
                const iconColor = isFolder ? (folderColors[item.cog_type] || folderColors.default) : 'text-gray-400';

                fileElement.dataset.id = item.id;
                fileElement.dataset.type = item.cog_type;
                fileElement.dataset.name = item.name;

                // deleted-files-filter (#1): in the Deleted view each row shows
                // its ORIGINAL location (the parent-folder chain it lived in
                // before deletion), so the user knows what they're restoring.
                // API supplies `original_path` on the deleted=true branch.
                const _origPath = (showDeleted && item.original_path) ? item.original_path : '';
                const _escOrigPath = _origPath.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
                const pathLineList = _origPath
                    ? `<div class="text-xs text-gray-400 truncate" title="Original location: ${_escOrigPath}">was in ${_escOrigPath}</div>`
                    : '';
                const pathLineGrid = _origPath
                    ? `<div class="mt-1 text-xs text-gray-400 break-words w-full px-1 line-clamp-1" title="Original location: ${_escOrigPath}">was in ${_escOrigPath}</div>`
                    : '';

                if (viewMode === 'list') {
                    // --- LIST VIEW ---
                    fileElement.className = `file-item group flex items-center px-3 py-2 cursor-pointer transition-colors duration-150 rounded-lg min-w-0 ${
                        isSelected
                            ? 'bg-primary/10 border border-primary'
                            : 'hover:bg-gray-50 border border-transparent'
                    }`;

                    const smallIcon = isFolder
                        ? `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="${iconColor}">
                             <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                           </svg>`
                        : `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="${iconColor}">
                             <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/>
                           </svg>`;

                    fileElement.innerHTML = `
                        <input type="checkbox" aria-label="Select file"
                               class="file-checkbox h-4 w-4 text-primary rounded border-gray-300 focus:ring-2 focus:ring-primary"
                               ${isSelected ? 'checked' : ''}>
                        <div class="w-6 mr-3 flex-shrink-0 flex justify-center">${smallIcon}</div>
                        <div class="min-w-0" style="flex:1 1 200px;">
                            <div class="flex items-center gap-2 min-w-0">
                                <div class="text-sm text-gray-900 truncate" title="${item.name}">${item.name}</div>
                                ${dataLabelChip(item.data_classification, { subtleDefault: false })}
                            </div>
                            ${pathLineList}
                        </div>
                        <div class="text-xs text-gray-500 text-right flex-shrink-0 hidden sm:block" style="width:80px;">${formatFileSize(item.size_bytes)}</div>
                        <div class="text-xs text-gray-500 text-right flex-shrink-0 hidden md:block" style="width:100px;">${formatDate(item.updated_at || item.created_at)}</div>
                        <div class="text-center flex-shrink-0" style="width:60px;">
                            <span class="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium ${
                                isFolder ? 'bg-primary/10 text-primary' : 'bg-gray-100 text-gray-600'
                            }">
                                ${isFolder ? 'Folder' : 'File'}
                            </span>
                        </div>
                    `;
                } else {
                    // --- GRID VIEW (original card design) ---
                    fileElement.className = `file-item group relative bg-white border-2 rounded-xl cursor-pointer transition-all duration-200 ${
                        isSelected
                            ? 'border-primary shadow-lg shadow-blue-100'
                            : 'border-gray-200 hover:border-gray-300 hover:shadow-md'
                    }`;

                    fileElement.innerHTML = `
                        <!-- Checkbox in top-left corner -->
                        <div class="absolute top-3 left-3 z-10">
                            <input type="checkbox" aria-label="Select file"
                                   class="file-checkbox h-5 w-5 text-primary rounded border-gray-300 focus:ring-2 focus:ring-primary"
                                   ${isSelected ? 'checked' : ''}>
                        </div>

                        <!-- Thumbnail area -->
                        <div class="thumbnail-slot flex items-center justify-center bg-gray-50 rounded-t-xl overflow-hidden" style="min-height: 80px;">
                            <div class="${iconColor} transition-transform group-hover:scale-110 default-icon">
                                ${isFolder
                                    ? `<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                         <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                                       </svg>`
                                    : `<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                         <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/>
                                       </svg>`
                                }
                            </div>
                        </div>

                        <!-- Card Content -->
                        <div class="p-3 flex flex-col items-center text-center">
                            <div class="flex items-center justify-center gap-1.5 flex-wrap">
                                <div class="name text-sm font-medium text-gray-900 break-words px-1 line-clamp-2" title="${item.name}">
                                    ${truncateName(item.name)}
                                </div>
                                ${dataLabelChip(item.data_classification, { subtleDefault: false })}
                            </div>
                            ${pathLineGrid}
                            <div class="mt-1.5">
                                <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
                                    isFolder ? 'bg-primary/10 text-primary' : 'bg-gray-100 text-gray-600'
                                }">
                                    ${isFolder ? '📁 Folder' : '📄 File'}
                                </span>
                            </div>
                        </div>
                    `;

                    // Load inline thumbnail for files (async, non-blocking).
                    // Soft-deleted files have no retrievable content — skip.
                    if (!isFolder && !showDeleted) {
                        const thumbSlot = fileElement.querySelector('.thumbnail-slot');
                        loadThumbnail(thumbSlot, item);
                    }
                }

                // Checkbox click toggles selection
                const checkbox = fileElement.querySelector('.file-checkbox');
                checkbox.addEventListener('click', (e) => {
                    e.stopPropagation();
                    handleFileSelection(fileElement, item);
                });

                // Click on item (not checkbox): a plain click selects ONLY this
                // item and switches the preview to it. Ctrl/Cmd-click adds to a
                // multi-selection. Previously every click just toggled (additive),
                // so selection stuck and the preview/permissions stayed on the
                // first-clicked file.
                fileElement.addEventListener('click', (e) => {
                    if (e.target.classList.contains('file-checkbox')) return;
                    const multi = e.ctrlKey || e.metaKey;
                    if (multi) {
                        handleFileSelection(fileElement, item);
                    } else {
                        // Clear any previous selection (state + visuals), then select just this.
                        selectedCogIds.clear();
                        selectedItems.clear();
                        document.querySelectorAll('#file-list [data-id]').forEach(el => {
                            el.classList.remove('border-primary', 'shadow-lg', 'shadow-blue-100', 'bg-primary/10');
                            const cb = el.querySelector('.file-checkbox');
                            if (cb) cb.checked = false;
                        });
                        handleFileSelection(fileElement, item);
                    }

                    // deleted-files-filter (#4): no preview/permissions panel for
                    // soft-deleted files — selection stays (so Restore can act),
                    // but there's nothing live to preview.
                    if (showDeleted) return;

                    _previewCogId = item.id;
                    _previewCogName = item.name;
                    _previewOwnerId = item.owner_id;
                    if (!isFolder) {
                        openPreview(item);
                        // Refresh the permissions list for THIS file so the panel
                        // doesn't keep showing the previously-opened file.
                        loadPreviewPermissions(item.id);
                    } else {
                        openFolderPermissionsPanel(item);
                    }
                });

                // Double click to navigate into folders (not in the Deleted view —
                // deleted-files-filter #4 — navigating into a deleted folder is
                // meaningless).
                if (isFolder && !showDeleted) {
                    fileElement.addEventListener('dblclick', () => {
                        selectedCogIds.clear();
                        selectedItems.clear();
                        closePreview();

                        breadcrumbTrail.push({ id: item.id, name: item.name });
                        loadFiles(item.id);
                    });
                }

                // Right-click context menu
                fileElement.addEventListener('contextmenu', (e) => {
                    e.preventDefault();
                    const isOwner = item.owner_id === _currentUserId;
                    const shareIcon = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg>';
                    const parentId = breadcrumbTrail.length > 0 ? breadcrumbTrail[breadcrumbTrail.length - 1].id : null;

                    // deleted-files-filter (#4): in the Deleted view the only
                    // meaningful action is Restore — the live actions (share,
                    // move, delete, download) don't apply to a soft-deleted file.
                    if (showDeleted) {
                        showContextMenu(e.clientX, e.clientY, [{
                            label: 'Restore',
                            icon: '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7v6h6"/><path d="M3 13a9 9 0 1 0 3-7.7L3 8"/></svg>',
                            action: () => handleRestoreFile(item),
                        }]);
                        return;
                    }

                    const menuItems = [];

                    if (isOwner) {
                        menuItems.push({
                            label: 'Share...',
                            icon: shareIcon,
                            action: () => openShareModal(item.id, item.name, { onSuccess: () => { loadFiles(parentId); if (_previewCogId === item.id) loadPreviewPermissions(item.id); } }),
                        });
                    } else {
                        menuItems.push({
                            label: 'Request Access...',
                            icon: '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/></svg>',
                            action: () => openRequestAccessModal(item.id, item.name),
                        });
                    }

                    menuItems.push({ divider: true });
                    menuItems.push({
                        label: 'Download',
                        icon: '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
                        action: () => { selectedCogIds.clear(); selectedItems.clear(); selectedCogIds.add(item.id); selectedItems.set(item.id, item); document.getElementById('download-selected-button')?.click(); },
                    });
                    menuItems.push({
                        label: 'Move to...',
                        icon: '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>',
                        action: () => { selectedCogIds.clear(); selectedItems.clear(); selectedCogIds.add(item.id); selectedItems.set(item.id, item); document.getElementById('move-selected-button')?.click(); },
                    });
                    menuItems.push({ divider: true });
                    menuItems.push({
                        label: 'Delete',
                        icon: '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
                        action: () => { selectedCogIds.clear(); selectedItems.clear(); selectedCogIds.add(item.id); selectedItems.set(item.id, item); document.getElementById('delete-selected-button')?.click(); },
                    });

                    showContextMenu(e.clientX, e.clientY, menuItems);
                });

                // Permission indicator (shared badge)
                if (item.direct_permission_count > 0) {
                    const badge = document.createElement('div');
                    badge.className = 'absolute bottom-2 right-2 flex items-center space-x-1 text-xs text-gray-500 bg-gray-100 rounded-full px-2 py-0.5';
                    badge.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg><span>${item.direct_permission_count}</span>`;
                    badge.title = `Shared with ${item.direct_permission_count} user(s)`;
                    if (viewMode === 'grid') {
                        fileElement.appendChild(badge);
                    }
                }

                // deleted-files-filter (#4): a visible, discoverable Restore
                // button on each soft-deleted row (in addition to the right-click
                // Restore). Calls POST /cogs/{id}/restore then reloads the view.
                if (showDeleted) {
                    const restoreBtn = document.createElement('button');
                    restoreBtn.type = 'button';
                    restoreBtn.className = 'restore-file-btn ' + (viewMode === 'list'
                        ? 'ml-3 shrink-0 px-2.5 py-1 text-xs font-medium text-primary border border-primary/40 rounded-md hover:bg-primary/10 transition-colors'
                        : 'absolute top-2 right-2 px-2.5 py-1 text-xs font-medium text-primary bg-white/90 border border-primary/40 rounded-md hover:bg-primary/10 transition-colors');
                    restoreBtn.textContent = 'Restore';
                    restoreBtn.title = 'Restore this file';
                    restoreBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        handleRestoreFile(item);
                    });
                    if (viewMode === 'grid') fileElement.classList.add('relative');
                    fileElement.appendChild(restoreBtn);
                }

                fileListContainer.appendChild(fileElement);
            });
        }
        renderBreadcrumbs();
        updateFileActionButtons();
        updateSelectAllCheckbox();

    } catch (error) {
        console.error("Error loading files:", error);
        fileListContainer.innerHTML = '<p class="text-center text-red-500 col-span-full">Could not load files.</p>';
    }
}

function handleFileSelection(element, item) {
    const cogId = parseInt(element.dataset.id);
    const checkbox = element.querySelector('.file-checkbox');

    if (selectedCogIds.has(cogId)) {
        // Deselect
        selectedCogIds.delete(cogId);
        selectedItems.delete(cogId);
        element.classList.remove('border-primary', 'shadow-lg', 'shadow-blue-100');
        element.classList.add('border-gray-200');
        if (checkbox) checkbox.checked = false;
    } else {
        // Select
        selectedCogIds.add(cogId);
        selectedItems.set(cogId, {
            id: item.id,
            name: item.name,
            type: item.cog_type
        });
        element.classList.remove('border-gray-200');
        element.classList.add('border-primary', 'shadow-lg', 'shadow-blue-100');
        if (checkbox) checkbox.checked = true;
    }
    updateFileActionButtons();
    updateSelectAllCheckbox();
}


function renderBreadcrumbs() {
    const breadcrumbContainer = document.getElementById('breadcrumb');
    if (!breadcrumbContainer) return;

    breadcrumbContainer.innerHTML = '<a href="#" data-id="null" class="hover:underline">Home</a>';

    breadcrumbTrail.forEach(part => {
        breadcrumbContainer.innerHTML += ` <span class="mx-2 text-gray-400">/</span> <a href="#" data-id="${part.id}" class="hover:underline">${part.name}</a>`;
    });
}

export function updateFileActionButtons() {
    const bulkActionsBar = document.getElementById('bulk-actions-bar');
    const selectedCount = document.getElementById('selected-count');
    const hasSelection = selectedCogIds.size > 0;
    const count = selectedCogIds.size;

    // Bulk actions bar is always visible

    // Update selected count text
    if (selectedCount) {
        selectedCount.textContent = `${count} item${count !== 1 ? 's' : ''} selected`;
    }
}

function updateSelectAllCheckbox() {
    const selectAllCheckbox = document.getElementById('select-all-checkbox');
    if (!selectAllCheckbox) return;

    const fileItems = document.querySelectorAll('.file-item');
    const totalItems = fileItems.length;
    const selectedCount = selectedCogIds.size;

    if (totalItems === 0) {
        selectAllCheckbox.checked = false;
        selectAllCheckbox.indeterminate = false;
    } else if (selectedCount === 0) {
        selectAllCheckbox.checked = false;
        selectAllCheckbox.indeterminate = false;
    } else if (selectedCount === totalItems) {
        selectAllCheckbox.checked = true;
        selectAllCheckbox.indeterminate = false;
    } else {
        selectAllCheckbox.checked = false;
        selectAllCheckbox.indeterminate = true;
    }
}

function handleSelectAll() {
    const selectAllCheckbox = document.getElementById('select-all-checkbox');
    const fileItems = document.querySelectorAll('.file-item');

    if (selectAllCheckbox.checked || selectAllCheckbox.indeterminate) {
        // Select all
        fileItems.forEach(element => {
            const cogId = parseInt(element.dataset.id);
            if (!selectedCogIds.has(cogId)) {
                selectedCogIds.add(cogId);
                selectedItems.set(cogId, {
                    id: element.dataset.id,
                    name: element.dataset.name,
                    type: element.dataset.type
                });
                element.classList.remove('border-gray-200');
                element.classList.add('border-primary', 'shadow-lg', 'shadow-blue-100');
                const checkbox = element.querySelector('.file-checkbox');
                if (checkbox) checkbox.checked = true;
            }
        });
        selectAllCheckbox.indeterminate = false;
        selectAllCheckbox.checked = true;
    } else {
        // Deselect all
        fileItems.forEach(element => {
            const cogId = parseInt(element.dataset.id);
            selectedCogIds.delete(cogId);
            selectedItems.delete(cogId);
            element.classList.remove('border-primary', 'shadow-lg', 'shadow-blue-100');
            element.classList.add('border-gray-200');
            const checkbox = element.querySelector('.file-checkbox');
            if (checkbox) checkbox.checked = false;
        });
    }

    updateFileActionButtons();
    updateSelectAllCheckbox();
}

// --- Modal Handlers ---
async function openCreateFolderModal() {
    const modal = await loadModal('make-folder');
    if (modal) {
        const form = modal.querySelector('#make-folder-form');
        const cancelButton = modal.querySelector('#cancel-folder-button');

        form.onsubmit = async (event) => {
            event.preventDefault();
            const folderName = document.getElementById('folder-name').value;
            // Bug-1: at Home, create the folder INSIDE the tenant root folder
            // (asset_folder) instead of a sibling master_folder that wouldn't
            // show at Home.
            const parentId = breadcrumbTrail.length > 0
                ? breadcrumbTrail[breadcrumbTrail.length - 1].id
                : await getRootFolderId();
            try {
                await api.cogs.create({ name: folderName, cog_type: 'asset_folder', parent_id: parentId });
                closeModal('make-folder-modal');
                loadFiles(parentId);
            } catch (error) {
                console.error("Error creating folder:", error);
                showError("Failed to create folder.");
            }
        };

        cancelButton.onclick = () => closeModal('make-folder-modal');
    }
}

async function openUploadFileModal() {
    const modal = await loadModal('upload-file');
    if (modal) {
        const form = modal.querySelector('#upload-file-form');
        const cancelButton = modal.querySelector('#cancel-upload-button');
        const uploadButton = modal.querySelector('#upload-submit-button');
        const fileInput = modal.querySelector('#file-input');
        const uploadProgress = modal.querySelector('#upload-progress');

        form.onsubmit = async (event) => {
            event.preventDefault();
            if (!fileInput.files || fileInput.files.length === 0) {
                showInfo("Please select a file to upload.");
                return;
            }
            // Bug-6: upload every selected file (e.g. an image sequence), not
            // just the first. Each file runs the per-file collision retry below.
            const filesToUpload = Array.from(fileInput.files);
            // Bug-1: at Home, upload INTO the tenant root folder so files are
            // visible, instead of relying on the backend's auto-root.
            const parentId = breadcrumbTrail.length > 0
                ? breadcrumbTrail[breadcrumbTrail.length - 1].id
                : await getRootFolderId();

            uploadButton.disabled = true;
            uploadButton.classList.add('opacity-50', 'cursor-not-allowed');
            const reEnable = () => {
                uploadButton.disabled = false;
                uploadButton.textContent = 'Upload';
                uploadButton.classList.remove('opacity-50', 'cursor-not-allowed');
            };

            let uploaded = 0, deduped = 0, failed = 0, skipped = 0;
            for (let i = 0; i < filesToUpload.length; i++) {
                const file = filesToUpload[i];
                uploadButton.textContent = filesToUpload.length > 1
                    ? `Uploading ${i + 1}/${filesToUpload.length}…` : 'Uploading…';
                uploadProgress.value = 0;
                const onProgress = (e) => {
                    if (e.lengthComputable) uploadProgress.value = (e.loaded / e.total) * 100;
                };
                // Bug-2: per-file rename/replace/cancel on a 409 name collision.
                let uploadOpts = {};
                let done = false;
                while (!done) {
                    try {
                        const result = await api.files.upload(file, parentId, onProgress, uploadOpts);
                        uploaded++;
                        if (result && result._deduped) deduped++;
                        done = true;
                    } catch (error) {
                        if (isNameCollisionError(error)) {
                            const choice = await resolveCollision(error);
                            if (!choice) { skipped++; done = true; break; }   // skip this file
                            if (choice.action === 'rename') { uploadOpts = { name: choice.new_name }; continue; }
                            if (choice.action === 'replace') { uploadOpts = { replace: true }; continue; }
                        }
                        console.error("Error uploading file:", error);
                        showError(`Failed to upload "${file.name}": ${error.message}`);
                        failed++;
                        done = true;
                    }
                }
            }

            closeModal('upload-file-modal');
            reEnable();
            if (uploaded > 0) {
                const parts = [`${uploaded} uploaded`];
                if (deduped > 0) parts.push(`${deduped} already in storage`);
                if (skipped > 0) parts.push(`${skipped} skipped`);
                if (failed > 0) parts.push(`${failed} failed`);
                showInfo(parts.join(', ') + '.');
            }
            loadFiles(parentId);
            loadStorageUsage();
            if (window.refreshOnboardingChecklist) window.refreshOnboardingChecklist();
        };
        cancelButton.onclick = () => closeModal('upload-file-modal');
    }
}


async function handleDownload() {
    if (selectedCogIds.size === 0) return;

    try {
        const itemsArray = Array.from(selectedItems.values());
        const cogIds = itemsArray.map(item => parseInt(item.id));
        const hasFolder = itemsArray.some(item => item.type.includes('folder'));

        // Single file without folder - use direct download (fast)
        if (selectedCogIds.size === 1 && !hasFolder) {
            await api.files.download(cogIds[0], false);
            return;
        }

        // Multiple items or folder - use async download job with progress modal
        await startAsyncDownload(cogIds);
    } catch (error) {
        console.error("Error downloading:", error);
        showError(`Failed to download: ${error.message}`);
    }
}

async function startAsyncDownload(cogIds) {
    // Open the progress modal
    const modal = await loadModal('download-progress');
    if (!modal) {
        showError('Failed to open download progress modal');
        return;
    }

    // Get UI elements
    const statusEl = document.getElementById('download-status');
    const progressBar = document.getElementById('download-progress-bar');
    const progressText = document.getElementById('download-progress-text');
    const currentFileEl = document.getElementById('download-current-file');
    const errorEl = document.getElementById('download-error');
    const errorMessageEl = document.getElementById('download-error-message');
    const successEl = document.getElementById('download-success');
    const closeButton = document.getElementById('close-download-button');
    const cancelButton = document.getElementById('cancel-download-button');
    const minimizeButton = document.getElementById('minimize-download-button');

    // Reset UI
    statusEl.textContent = 'Creating download job...';
    progressBar.style.width = '0%';
    progressText.textContent = '0%';
    currentFileEl.textContent = '';
    errorEl.classList.add('hidden');
    successEl.classList.add('hidden');
    closeButton.classList.add('hidden');
    cancelButton.classList.remove('hidden');
    isDownloadModalMinimized = false;

    // Set up button handlers
    cancelButton.onclick = () => {
        stopPolling();
        closeModal('download-progress-modal');
    };

    closeButton.onclick = () => {
        closeModal('download-progress-modal');
    };

    minimizeButton.onclick = () => {
        isDownloadModalMinimized = true;
        closeModal('download-progress-modal');
        showMinimizedNotification();
    };

    try {
        // Create the download job
        const jobResponse = await api.downloadJobs.create(cogIds);
        activeDownloadJobId = jobResponse.id;

        statusEl.textContent = 'Preparing download...';

        // Start polling for progress
        startPolling(jobResponse.id);

    } catch (error) {
        console.error('Error creating download job:', error);
        statusEl.textContent = 'Failed to create download job';
        errorEl.classList.remove('hidden');
        errorMessageEl.textContent = error.message || 'Unknown error occurred';
        cancelButton.classList.add('hidden');
        closeButton.classList.remove('hidden');
    }
}

function startPolling(jobId) {
    // Poll every 1 second
    downloadPollInterval = setInterval(async () => {
        try {
            const status = await api.downloadJobs.getStatus(jobId);
            updateDownloadProgress(status);

            // Check for terminal states
            if (status.status === 'completed') {
                stopPolling();
                handleDownloadComplete(status);
            } else if (status.status === 'failed') {
                stopPolling();
                handleDownloadFailed(status);
            }
        } catch (error) {
            console.error('Error polling download status:', error);
            // Don't stop polling on transient errors
        }
    }, 1000);
}

function stopPolling() {
    if (downloadPollInterval) {
        clearInterval(downloadPollInterval);
        downloadPollInterval = null;
    }
    activeDownloadJobId = null;
}

function updateDownloadProgress(status) {
    const statusEl = document.getElementById('download-status');
    const progressBar = document.getElementById('download-progress-bar');
    const progressText = document.getElementById('download-progress-text');
    const currentFileEl = document.getElementById('download-current-file');

    if (!statusEl) return; // Modal might be minimized

    // Update status message based on job status
    const statusMessages = {
        'pending': 'Waiting to start...',
        'preparing': 'Preparing files...',
        'downloading': `Downloading files (${status.processed_files}/${status.total_files})...`,
        'creating_zip': 'Creating zip archive...',
        'uploading': 'Uploading to storage...',
        'completed': 'Download ready!',
        'failed': 'Download failed'
    };
    statusEl.textContent = statusMessages[status.status] || 'Processing...';

    // Update progress bar
    const percent = status.progress_percent || 0;
    progressBar.style.width = `${percent}%`;
    progressText.textContent = `${percent}%`;

    // Update current file if available
    if (status.current_file) {
        currentFileEl.textContent = status.current_file;
        currentFileEl.title = status.current_file;
    }
}

function handleDownloadComplete(status) {
    const statusEl = document.getElementById('download-status');
    const successEl = document.getElementById('download-success');
    const closeButton = document.getElementById('close-download-button');
    const cancelButton = document.getElementById('cancel-download-button');
    const progressBar = document.getElementById('download-progress-bar');
    const progressText = document.getElementById('download-progress-text');

    // Update UI to show success
    if (statusEl) statusEl.textContent = 'Download ready!';
    if (progressBar) progressBar.style.width = '100%';
    if (progressText) progressText.textContent = '100%';
    if (successEl) successEl.classList.remove('hidden');
    if (cancelButton) cancelButton.classList.add('hidden');
    if (closeButton) closeButton.classList.remove('hidden');

    // Trigger the actual download
    if (status.download_url) {
        const a = document.createElement('a');
        a.style.display = 'none';
        a.href = status.download_url;
        a.download = status.filename || 'download.zip';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }

    // If minimized, show completion notification
    if (isDownloadModalMinimized) {
        showCompletionNotification(status.filename);
    }
}

function handleDownloadFailed(status) {
    const statusEl = document.getElementById('download-status');
    const errorEl = document.getElementById('download-error');
    const errorMessageEl = document.getElementById('download-error-message');
    const closeButton = document.getElementById('close-download-button');
    const cancelButton = document.getElementById('cancel-download-button');

    if (statusEl) statusEl.textContent = 'Download failed';
    if (errorEl) errorEl.classList.remove('hidden');
    if (errorMessageEl) errorMessageEl.textContent = status.error_message || 'Unknown error occurred';
    if (cancelButton) cancelButton.classList.add('hidden');
    if (closeButton) closeButton.classList.remove('hidden');

    // If minimized, show error notification
    if (isDownloadModalMinimized) {
        showErrorNotification(status.error_message);
    }
}

function showMinimizedNotification() {
    // Create a small notification in the corner
    const existingNotif = document.getElementById('download-minimized-notif');
    if (existingNotif) existingNotif.remove();

    const notif = document.createElement('div');
    notif.id = 'download-minimized-notif';
    notif.className = 'fixed bottom-4 right-4 bg-primary text-white px-4 py-3 rounded-lg shadow-lg cursor-pointer z-50 flex items-center space-x-3';
    notif.innerHTML = `
        <div class="animate-spin h-4 w-4 border-2 border-white border-t-transparent rounded-full"></div>
        <span>Preparing download...</span>
    `;
    notif.onclick = async () => {
        notif.remove();
        isDownloadModalMinimized = false;
        // Re-open the modal
        const modal = await loadModal('download-progress');
        if (modal && activeDownloadJobId) {
            // Get latest status and update UI
            try {
                const status = await api.downloadJobs.getStatus(activeDownloadJobId);
                updateDownloadProgress(status);

                // Set up button handlers again
                const cancelButton = document.getElementById('cancel-download-button');
                const closeButton = document.getElementById('close-download-button');
                const minimizeButton = document.getElementById('minimize-download-button');

                if (cancelButton) {
                    cancelButton.onclick = () => {
                        stopPolling();
                        closeModal('download-progress-modal');
                    };
                }
                if (closeButton) {
                    closeButton.onclick = () => {
                        closeModal('download-progress-modal');
                    };
                }
                if (minimizeButton) {
                    minimizeButton.onclick = () => {
                        isDownloadModalMinimized = true;
                        closeModal('download-progress-modal');
                        showMinimizedNotification();
                    };
                }

                // Check for terminal states
                if (status.status === 'completed') {
                    handleDownloadComplete(status);
                } else if (status.status === 'failed') {
                    handleDownloadFailed(status);
                }
            } catch (error) {
                console.error('Error getting download status:', error);
            }
        }
    };
    document.body.appendChild(notif);
}

function showCompletionNotification(filename) {
    const notif = document.getElementById('download-minimized-notif');
    if (notif) {
        notif.innerHTML = `
            <svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
            </svg>
            <span>Download complete: ${filename || 'download.zip'}</span>
        `;
        notif.className = 'fixed bottom-4 right-4 bg-green-600 text-white px-4 py-3 rounded-lg shadow-lg cursor-pointer z-50 flex items-center space-x-3';
        // Auto-hide after 5 seconds
        setTimeout(() => notif.remove(), 5000);
    }
}

function showErrorNotification(errorMessage) {
    const notif = document.getElementById('download-minimized-notif');
    if (notif) {
        notif.innerHTML = `
            <svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
            </svg>
            <span>Download failed</span>
        `;
        notif.className = 'fixed bottom-4 right-4 bg-red-600 text-white px-4 py-3 rounded-lg shadow-lg cursor-pointer z-50 flex items-center space-x-3';
        // Auto-hide after 5 seconds
        setTimeout(() => notif.remove(), 5000);
    }
}

// --- Folder Picker Modal ---
async function openFolderPicker(callback) {
    folderPickerCallback = callback;
    // Open the picker at the user's CURRENT folder so the default
    // "where to copy/move" is "right here" (matches how every
    // desktop file manager works). Falls back to root if the user
    // is at root or has no breadcrumb yet.
    folderPickerTrail = breadcrumbTrail.map(p => ({ id: p.id, name: p.name }));
    selectedFolderDestination = null;

    const modal = await loadModal('folder-picker');
    if (modal) {
        const cancelButton = modal.querySelector('#cancel-folder-picker-button');
        const confirmButton = modal.querySelector('#confirm-folder-picker-button');
        const upButton = modal.querySelector('#folder-picker-up-button');

        cancelButton.onclick = () => {
            folderPickerCallback = null;
            selectedFolderDestination = null;
            closeModal('folder-picker-modal');
        };

        confirmButton.onclick = () => {
            if (folderPickerCallback && selectedFolderDestination !== null) {
                folderPickerCallback(selectedFolderDestination.id);
            }
            folderPickerCallback = null;
            selectedFolderDestination = null;
            closeModal('folder-picker-modal');
        };

        upButton.onclick = () => {
            if (folderPickerTrail.length > 0) {
                folderPickerTrail.pop();
                const parentId = folderPickerTrail.length > 0
                    ? folderPickerTrail[folderPickerTrail.length - 1].id
                    : null;
                loadFolderPickerContents(parentId);
            }
        };

        // Open at the user's current folder (or root if they're at root).
        const startParentId = folderPickerTrail.length > 0
            ? folderPickerTrail[folderPickerTrail.length - 1].id
            : null;
        loadFolderPickerContents(startParentId);
    }
}

async function loadFolderPickerContents(parentId) {
    const folderListContainer = document.getElementById('folder-picker-list');
    const breadcrumbDisplay = document.getElementById('folder-picker-breadcrumb');

    if (!folderListContainer) return;

    try {
        // Fetch folders at the current level (just like the main files UI does)
        const endpoint = parentId ? `/assets/?parent_id=${parentId}` : '/assets/';
        const allItems = await api.fetchApi(endpoint);
        const foldersAtCurrentLevel = (allItems || []).filter(item => item && (item.cog_type || '').includes('folder'));

        console.log(`Fetching from: ${endpoint}`);
        console.log('Items at this level:', allItems);
        console.log('Folders at this level:', foldersAtCurrentLevel);

        // Update breadcrumb
        updateFolderPickerBreadcrumb();

        // Automatically select the current location as the destination
        selectFolderDestination(parentId, getCurrentPath());

        // Clear and rebuild folder list
        folderListContainer.innerHTML = '';

        // Show folders at current level
        if (foldersAtCurrentLevel.length === 0) {
            folderListContainer.innerHTML += '<p class="text-center text-gray-500 py-4">No subfolders here. Click "Confirm Selection" to choose this location, or use the up button to go back.</p>';
        } else {
            foldersAtCurrentLevel.forEach(folder => {
                const folderElement = document.createElement('div');
                folderElement.className = 'folder-picker-item p-3 border-2 border-gray-200 rounded-lg mb-2 flex items-center cursor-pointer hover:border-primary hover:bg-primary/15 transition-colors';
                folderElement.dataset.id = folder.id;
                folderElement.dataset.name = folder.name;

                // Color code based on folder type
                const folderColor = folder.cog_type === 'master_folder' ? 'text-primary' : 'text-primary';

                folderElement.innerHTML = `
                    <div class="${folderColor} mr-3">
                        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                        </svg>
                    </div>
                    <div class="flex-1">
                        <div class="font-medium text-gray-900" title="${folder.name}">${truncateName(folder.name)}</div>
                        <div class="text-xs text-gray-500">${folder.cog_type === 'master_folder' ? 'Master Folder' : 'Asset Folder'}</div>
                    </div>
                    <div class="text-gray-400">
                        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <polyline points="9 18 15 12 9 6"/>
                        </svg>
                    </div>
                `;

                // Double-click to navigate into folder
                folderElement.ondblclick = () => {
                    folderPickerTrail.push({ id: folder.id, name: folder.name });
                    loadFolderPickerContents(folder.id);
                };

                folderListContainer.appendChild(folderElement);
            });

            // Add hint
            const hintElement = document.createElement('div');
            hintElement.className = 'text-xs text-gray-500 text-center mt-4 italic';
            hintElement.textContent = 'Double-click a folder to navigate into it';
            folderListContainer.appendChild(hintElement);
        }

    } catch (error) {
        console.error("Error loading folder picker contents:", error);
        folderListContainer.innerHTML = '<p class="text-center text-red-500">Could not load folders.</p>';
    }
}

function updateFolderPickerBreadcrumb() {
    const breadcrumbDisplay = document.getElementById('folder-picker-breadcrumb');
    if (!breadcrumbDisplay) return;

    // Build breadcrumb
    let breadcrumbHTML = '<span class="text-gray-900 font-medium cursor-pointer hover:text-primary/80" data-folder-id="null">Home</span>';

    folderPickerTrail.forEach((folder, index) => {
        breadcrumbHTML += ` <span class="text-gray-400 mx-1">/</span> `;
        breadcrumbHTML += `<span class="text-gray-900 font-medium cursor-pointer hover:text-primary/80" data-folder-id="${folder.id}">${folder.name}</span>`;
    });

    breadcrumbDisplay.innerHTML = breadcrumbHTML;

    // Add click handlers to breadcrumb parts
    breadcrumbDisplay.querySelectorAll('[data-folder-id]').forEach(crumb => {
        crumb.onclick = () => {
            const targetId = crumb.dataset.folderId === 'null' ? null : parseInt(crumb.dataset.folderId);

            // Find position in trail
            if (targetId === null) {
                folderPickerTrail = [];
                loadFolderPickerContents(null);
            } else {
                const partIndex = folderPickerTrail.findIndex(f => f.id === targetId);
                if (partIndex >= 0) {
                    folderPickerTrail = folderPickerTrail.slice(0, partIndex + 1);
                    loadFolderPickerContents(targetId);
                }
            }
        };
    });
}

function getCurrentPath() {
    if (folderPickerTrail.length === 0) return 'Home';
    return 'Home / ' + folderPickerTrail.map(f => f.name).join(' / ');
}

function selectFolderDestination(folderId, folderPath) {
    selectedFolderDestination = { id: folderId, path: folderPath };

    // Update selection display
    const selectedPathDisplay = document.getElementById('folder-picker-selected-path');
    if (selectedPathDisplay) {
        selectedPathDisplay.textContent = folderPath;
    }
}

// --- Folder Permissions Panel ---
function openFolderPermissionsPanel(item) {
    const panel = document.getElementById('preview-panel');
    if (!panel) return;

    // Show active preview, hide placeholder
    const placeholder = document.getElementById('preview-placeholder');
    const active = document.getElementById('preview-active');
    if (placeholder) placeholder.classList.add('hidden');
    if (active) active.classList.remove('hidden');

    const filename = document.getElementById('preview-filename');
    if (filename) filename.textContent = item.name;

    // Switch to Permissions tab
    document.querySelectorAll('.preview-tab').forEach(t => {
        t.classList.remove('text-primary', 'border-b-2', 'border-primary');
        t.classList.add('text-gray-500');
    });
    const permTab = document.querySelector('.preview-tab[data-tab="permissions"]');
    if (permTab) {
        permTab.classList.remove('text-gray-500');
        permTab.classList.add('text-primary', 'border-b-2', 'border-primary');
    }

    document.getElementById('preview-tab-content')?.classList.add('hidden');
    document.getElementById('permissions-tab-content')?.classList.remove('hidden');

    // Hide the file preview content, show a folder/file icon + name.
    const previewContent = document.getElementById('preview-content');
    if (previewContent) {
        const isFolderItem = !!(item.cog_type && item.cog_type.toLowerCase().includes('folder'));
        const icon = isFolderItem
            ? '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="text-yellow-500 mb-2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'
            : '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="text-gray-400 mb-2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
        previewContent.innerHTML = `
            <div class="flex flex-col items-center justify-center h-full text-gray-400">
                ${icon}
                <span class="text-sm">${item.name}</span>
                <span class="text-xs mt-1">${isFolderItem ? 'Folder — changes here apply to all contents' : 'File — changes here apply to this file only'}</span>
            </div>
        `;
    }

    loadPreviewPermissions(item.id);
}

// --- Request Access Modal ---
function openRequestAccessModal(cogId, cogName) {
    const existing = document.getElementById('request-access-modal');
    if (existing) existing.remove();

    const modalHtml = `
        <div id="request-access-modal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50 flex items-center justify-center">
            <div class="relative mx-auto p-6 border w-full max-w-md shadow-lg rounded-md bg-white">
                <div class="flex justify-between items-center pb-3 border-b">
                    <h3 class="text-lg font-semibold text-gray-900">Request Access: ${cogName}</h3>
                    <button onclick="document.getElementById('request-access-modal').remove()"
                            class="text-gray-400 hover:text-gray-600 text-2xl leading-none" aria-label="Close">&times;</button>
                </div>
                <div class="mt-4 space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-2">Access Level</label>
                        <div class="grid grid-cols-2 gap-2">
                            <button type="button" class="req-role-btn border-2 border-gray-300 rounded-lg p-3 text-center hover:border-primary hover:bg-primary/15 transition-colors" data-role="viewer">
                                <div class="text-xl mb-1">&#128065;</div>
                                <div class="text-sm font-medium">Viewer</div>
                                <div class="text-xs text-gray-500">View & download</div>
                            </button>
                            <button type="button" class="req-role-btn border-2 border-gray-300 rounded-lg p-3 text-center hover:border-primary hover:bg-primary/15 transition-colors" data-role="editor">
                                <div class="text-xl mb-1">&#9999;</div>
                                <div class="text-sm font-medium">Editor</div>
                                <div class="text-xs text-gray-500">View, upload, edit</div>
                            </button>
                        </div>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1">Reason (optional)</label>
                        <textarea id="req-justification" aria-label="Reason (optional)" rows="2" placeholder="Why do you need access?"
                                  class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm"></textarea>
                    </div>
                </div>
                <div class="flex justify-end mt-4 space-x-2">
                    <button onclick="document.getElementById('request-access-modal').remove()"
                            class="px-4 py-2 bg-gray-200 text-gray-700 text-sm rounded hover:bg-gray-300">Cancel</button>
                    <button id="req-submit-btn" disabled
                            class="px-4 py-2 bg-primary text-white text-sm rounded hover:bg-primary/90 disabled:opacity-50">Request Access</button>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modalHtml);

    let selectedRole = '';
    document.querySelectorAll('.req-role-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.req-role-btn').forEach(b => {
                b.classList.remove('border-primary', 'bg-primary/10');
                b.classList.add('border-gray-300');
            });
            btn.classList.remove('border-gray-300');
            btn.classList.add('border-primary', 'bg-primary/10');
            selectedRole = btn.dataset.role;
            document.getElementById('req-submit-btn').disabled = false;
        });
    });

    document.getElementById('req-submit-btn').addEventListener('click', async () => {
        if (!selectedRole) return;
        const btn = document.getElementById('req-submit-btn');
        btn.disabled = true;
        btn.textContent = 'Requesting...';

        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem("accessToken");

        try {
            const resp = await fetch(`${apiUrl}/permission-requests`, {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    cog_id: cogId,
                    requested_level: selectedRole,
                    justification: document.getElementById('req-justification')?.value?.trim() || null,
                }),
            });

            if (!resp.ok) {
                const err = await resp.json();
                throw new Error(err.detail || 'Request failed');
            }

            document.getElementById('request-access-modal').remove();
            showInfo('Access request sent! The owner will be notified.');
        } catch (err) {
            showInfo(err.message);
            btn.disabled = false;
            btn.textContent = 'Request Access';
        }
    });

    document.addEventListener('keydown', function esc(e) {
        if (e.key === 'Escape') {
            document.getElementById('request-access-modal')?.remove();
            document.removeEventListener('keydown', esc);
        }
    });
}

// --- Permissions in Preview Panel ---
async function loadPreviewPermissions(cogId) {
    const list = document.getElementById('preview-permissions-list');
    const actionsDiv = document.getElementById('preview-perm-actions');
    if (!list) return;

    // Render action button based on ownership
    if (actionsDiv) {
        const isOwner = _previewOwnerId === _currentUserId;
        if (isOwner) {
            actionsDiv.innerHTML = '<button id="preview-share-btn" class="px-3 py-1 bg-primary text-white text-xs rounded-lg hover:bg-primary/90">Share</button>';
            actionsDiv.querySelector('#preview-share-btn')?.addEventListener('click', () => {
                // If the previewed file is part of a multi-file checkbox
                // selection, Share must act on ALL of them — otherwise the
                // selection is silently ignored and only this file gets shared
                // (the reported bug). Fall through to single-file share only
                // when there's no active multi-selection of this file.
                if (selectedCogIds.size > 1 && _previewCogId && selectedCogIds.has(_previewCogId)) {
                    handleBulkShare();
                    return;
                }
                if (_previewCogId && _previewCogName) {
                    openShareModal(_previewCogId, _previewCogName, {
                        onSuccess: () => loadPreviewPermissions(_previewCogId),
                    });
                }
            });
        } else {
            actionsDiv.innerHTML = '<button id="preview-request-btn" class="px-3 py-1 bg-primary text-white text-xs rounded-lg hover:bg-primary/90">Request Access</button>';
            actionsDiv.querySelector('#preview-request-btn')?.addEventListener('click', () => {
                if (_previewCogId && _previewCogName) {
                    openRequestAccessModal(_previewCogId, _previewCogName);
                }
            });
        }
    }

    list.innerHTML = '<p class="text-sm text-gray-500">Loading...</p>';

    const isOwner = _previewOwnerId === _currentUserId;

    try {
        // include_inherited=true so folder-level grants show here (read-only),
        // distinct from direct per-file grants — otherwise a child file looks
        // unshared even though it's reachable via its parent folder.
        const permList = (await loadPermissionsForCog(cogId, true)) || [];

        // Is this cog a folder? (has children) — drives the cascade warning, the
        // scope banner, and the per-item drill-down. One fetch, reused below.
        let _kids = [];
        try { _kids = (await api.cogs.get(cogId, true)) || []; } catch (e) { _kids = []; }
        const isFolderCog = _kids.length > 0;

        list.innerHTML = '';

        // Scope banner — make it unmistakable what a change here affects.
        if (isOwner) {
            const banner = document.createElement('div');
            if (isFolderCog) {
                banner.className = 'text-xs rounded px-2 py-1.5 mb-2 bg-yellow-50 text-yellow-800 border border-yellow-200';
                banner.innerHTML = `🗂 <b>Folder</b> (#${cogId}) — a change here applies to <b>everything inside</b>. To change one item, use the list at the bottom.`;
            } else {
                banner.className = 'text-xs rounded px-2 py-1.5 mb-2 bg-blue-50 text-blue-800 border border-blue-200';
                banner.innerHTML = `📄 <b>This file only</b> (#${cogId}) — changes here don't affect other files.`;
            }
            list.appendChild(banner);
        }

        if (permList.length === 0 && !isFolderCog) {
            const none = document.createElement('p');
            none.className = 'text-sm text-gray-500';
            none.textContent = 'No one has been granted access yet.';
            list.appendChild(none);
            return;
        }
        const perms = permList;
        // A user can appear twice: a direct grant on THIS item plus an inherited
        // grant from a parent folder. The direct one wins (it's what's effective
        // and editable), so hide the duplicate inherited row to avoid the
        // "it changed everywhere" confusion.
        const directUserIds = new Set(perms.filter(p => !p.is_inherited && p.user_id).map(p => p.user_id));
        perms.forEach(p => {
            if (p.is_inherited && p.user_id && directUserIds.has(p.user_id)) return;
            const name = p.user_name || p.user_email || p.team_name || `User ${p.user_id}`;
            const role = p.level || 'viewer';
            const tenantTag = p.tenant_name ? ` [${p.tenant_name}]` : '';

            const row = document.createElement('div');
            row.className = 'flex items-center justify-between py-2 border-b border-gray-100 last:border-0';

            const nameDiv = document.createElement('div');
            nameDiv.className = 'text-sm text-gray-700 min-w-0 flex-1';
            const tenantDisplay = p.tenant_name ? `<span class="text-xs text-primary"> [${p.tenant_name} #${p.tenant_id}]</span>` : '';
            nameDiv.innerHTML = `<span class="truncate">${name}</span><span class="text-xs text-gray-400"> #${p.user_id}</span>${tenantDisplay}`;
            if (p.is_inherited) {
                const badge = document.createElement('span');
                badge.className = 'text-xs text-gray-400 ml-1 italic';
                badge.textContent = '(inherited from parent)';
                nameDiv.appendChild(badge);
            }
            // Expiry indicator (bug 13): make the expiration window visible.
            if (p.expires_at) {
                const exp = document.createElement('div');
                const d = new Date(p.expires_at);
                const when = d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
                if (p.is_expired) {
                    exp.className = 'text-xs text-red-500';
                    exp.textContent = `Expired ${when}`;
                } else {
                    exp.className = 'text-xs text-amber-600';
                    exp.textContent = `Expires ${when}`;
                }
                nameDiv.appendChild(exp);
            }
            row.appendChild(nameDiv);

            if (isOwner && !p.is_inherited) {
                const select = document.createElement('select');
                select.className = 'border border-gray-300 rounded px-1.5 py-0.5 text-xs ml-2 flex-shrink-0';
                select.innerHTML = `
                    <option value="viewer" ${role === 'viewer' ? 'selected' : ''}>Viewer</option>
                    <option value="editor" ${role === 'editor' ? 'selected' : ''}>Editor</option>
                    <option value="owner" ${role === 'owner' ? 'selected' : ''}>Owner</option>
                `;
                select.addEventListener('change', async () => {
                    // Guard the cascade: changing a FOLDER's grant applies to all
                    // contents. Make the user confirm so it can't happen by accident
                    // (this is the bug where "changing one file" hit the folder).
                    if (isFolderCog) {
                        const ok = await showConfirm(`This changes access to the whole folder "${_previewCogName}" and everything inside it (${_kids.length} item${_kids.length === 1 ? '' : 's'}).\n\nTo change just ONE file, click Cancel and use the "Set permissions for one item" list at the bottom of this panel.\n\nApply to the whole folder?`);
                        if (!ok) { loadPreviewPermissions(cogId); return; }
                    }
                    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
                    const token = sessionStorage.getItem("accessToken");
                    try {
                        const resp = await fetch(`${apiUrl}/permissions/${p.id}`, {
                            method: 'PATCH',
                            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
                            body: JSON.stringify({ level: select.value }),
                        });
                        if (resp.ok) {
                            select.style.borderColor = '#22c55e';
                            // Reload after the confirmation flash so the list
                            // reflects exactly what the server stored (this item
                            // only — never touches siblings).
                            setTimeout(() => loadPreviewPermissions(cogId), 800);
                        } else {
                            showError('Failed to update permission');
                            loadPreviewPermissions(cogId);
                        }
                    } catch (e) {
                        showError('Error: ' + e.message);
                    }
                });

                const revokeBtn = document.createElement('button');
                revokeBtn.className = 'text-red-500 hover:text-red-700 text-xs ml-2 flex-shrink-0';
                revokeBtn.textContent = 'x';
                revokeBtn.title = 'Revoke';
                revokeBtn.addEventListener('click', async () => {
                    if (!await showConfirm(`Revoke access for ${name}?`)) return;
                    try {
                        await revokePermission(p.id);
                        loadPreviewPermissions(cogId);
                    } catch (e) {
                        showError('Failed to revoke');
                    }
                });

                row.appendChild(select);
                row.appendChild(revokeBtn);
            } else if (isOwner && p.is_inherited && p.user_id) {
                // Inherited from a parent folder. Changing it here creates a
                // per-item OVERRIDE (a direct grant on THIS item) instead of
                // editing the folder's grant — so it won't cascade to siblings.
                const ovSelect = document.createElement('select');
                ovSelect.className = 'border border-amber-300 bg-amber-50 rounded px-1.5 py-0.5 text-xs ml-2 flex-shrink-0';
                ovSelect.title = "Inherited from a parent folder. Changing this sets a permission for THIS item only — it won't affect other items in the folder.";
                ovSelect.innerHTML = `
                    <option value="viewer" ${role === 'viewer' ? 'selected' : ''}>Viewer (inherited)</option>
                    <option value="editor" ${role === 'editor' ? 'selected' : ''}>Editor</option>
                    <option value="owner" ${role === 'owner' ? 'selected' : ''}>Owner</option>
                `;
                ovSelect.addEventListener('change', async () => {
                    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
                    const token = sessionStorage.getItem("accessToken");
                    try {
                        const resp = await fetch(`${apiUrl}/permissions/cogs/${cogId}`, {
                            method: 'POST',
                            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
                            body: JSON.stringify({ target_type: 'cog', target_id: cogId, user_id: p.user_id, level: ovSelect.value }),
                        });
                        if (resp.ok) {
                            ovSelect.style.borderColor = '#22c55e';
                            setTimeout(() => loadPreviewPermissions(cogId), 800);
                        } else {
                            showError('Failed to set override');
                            loadPreviewPermissions(cogId);
                        }
                    } catch (e) { showError('Error: ' + e.message); }
                });
                row.appendChild(ovSelect);
            } else {
                const roleSpan = document.createElement('span');
                roleSpan.className = 'text-xs text-gray-500 capitalize flex-shrink-0 ml-2';
                roleSpan.textContent = role;
                row.appendChild(roleSpan);
            }

            list.appendChild(row);
        });

        // If this cog is a folder, show its contents as a reliable drill-down so
        // you can set ONE item differently without hunting in the grid. Clicking
        // a child opens THAT item's permissions (where inherited grants are
        // editable as a per-item override).
        if (isOwner) {
            try {
                const kids = _kids.filter(c => c && c.id);
                if (kids.length > 0) {
                    const sep = document.createElement('div');
                    sep.className = 'mt-4 pt-3 border-t border-gray-200';
                    sep.innerHTML = '<div class="text-xs font-semibold text-gray-500 mb-2">Set permissions for one item inside this folder:</div>';
                    const wrap = document.createElement('div');
                    wrap.className = 'space-y-0.5 max-h-48 overflow-y-auto';
                    kids.forEach(ch => {
                        const isF = !!(ch.cog_type && ch.cog_type.toLowerCase().includes('folder'));
                        const b = document.createElement('button');
                        b.className = 'w-full text-left text-sm text-gray-700 hover:bg-primary/5 rounded px-2 py-1 flex items-center gap-1.5';
                        b.innerHTML = `<span>${isF ? '🗂' : '📄'}</span><span class="truncate flex-1">${ch.name}</span><span class="text-xs text-primary flex-shrink-0">manage →</span>`;
                        b.addEventListener('click', () => {
                            _previewCogId = ch.id;
                            _previewCogName = ch.name;
                            _previewOwnerId = ch.owner_id;
                            openFolderPermissionsPanel(ch);
                        });
                        wrap.appendChild(b);
                    });
                    sep.appendChild(wrap);
                    list.appendChild(sep);
                }
            } catch (e) { /* non-fatal: drill-down is best-effort */ }
        }
    } catch (e) {
        list.innerHTML = '<p class="text-sm text-red-500">Failed to load permissions.</p>';
    }
}

// --- Share Handler ---
async function handleBulkShare() {
    if (selectedCogIds.size === 0) return;
    const ids = Array.from(selectedCogIds);
    const names = Array.from(selectedItems.values()).map(i => i.name).join(', ');
    const label = ids.length === 1 ? names : `${ids.length} items`;

    openShareModal(ids[0], label, {
        // grant = { userId, level, expiresAt } resolved by the modal. Passed in
        // explicitly because the modal DOM is gone by the time this fires —
        // reading #share-user-select here used to return null and silently drop
        // every item except the first.
        onSuccess: async (grant) => {
            // First item already shared by the modal — fan out the rest
            // through the aggregator so partial-failure surfaces.
            if (ids.length > 1 && grant && grant.userId && grant.level) {
                const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
                const token = sessionStorage.getItem("accessToken");

                const restItems = ids
                    .slice(1)
                    .map((id) => ({ id, name: (selectedItems.get(id) || {}).name || `Item #${id}` }));

                const { skipped } = await runBulkAction({
                    items: restItems,
                    action: (item) =>
                        bulkActionFetch(`${apiUrl}/permissions/cogs/${item.id}`, {
                            method: 'POST',
                            headers: {
                                'Authorization': `Bearer ${token}`,
                                'Content-Type': 'application/json',
                            },
                            body: JSON.stringify({
                                target_type: 'cog',
                                target_id: item.id,
                                user_id: parseInt(grant.userId),
                                level: grant.level,
                                ...(grant.expiresAt ? { expires_at: grant.expiresAt } : {}),
                            }),
                        }),
                });

                if (skipped.length > 0) {
                    showBulkSkipWarning({ verb: 'share', skippedItems: skipped });
                }
            }
            loadFiles((breadcrumbTrail.length > 0 ? breadcrumbTrail[breadcrumbTrail.length - 1].id : null));
            // Bug-A: if a shared cog is open in the preview, refresh its
            // "who has access" list so the new grant shows without reopening.
            if (_previewCogId && ids.includes(_previewCogId)) loadPreviewPermissions(_previewCogId);
        },
    });
}

// --- Move and Copy Handlers ---
async function handleRenameFiles() {
    // P3 — rename a single selected cog. The PATCH /cogs/{id} backend
    // already supports name updates; this is the UI affordance.
    if (selectedCogIds.size === 0) return;
    if (selectedCogIds.size > 1) {
        showInfo("Rename works on one item at a time. Deselect others or use Move for bulk operations.");
        return;
    }
    const item = Array.from(selectedItems.values())[0];
    if (!item) return;

    const currentName = item.name || "";
    const newName = window.prompt(`Rename "${currentName}" to:`, currentName);
    if (newName === null) return;            // user cancelled
    const trimmed = newName.trim();
    if (!trimmed) {
        showInfo("Name cannot be empty.");
        return;
    }
    if (trimmed === currentName) return;     // no-op

    let attemptName = trimmed;
    let payload = { name: attemptName };
    while (true) {
        try {
            await api.cogs.update(item.id, payload);
            showInfo(`Renamed to "${attemptName}".`);
            selectedCogIds.clear();
            selectedItems.clear();
            const parentId = breadcrumbTrail.length > 0
                ? breadcrumbTrail[breadcrumbTrail.length - 1].id
                : null;
            loadFiles(parentId);
            return;
        } catch (error) {
            if (isNameCollisionError(error)) {
                const choice = await resolveCollision(error);
                if (!choice) return;  // cancelled
                if (choice.action === "rename") {
                    attemptName = choice.new_name;
                    payload = { name: attemptName };
                    continue;
                }
                if (choice.action === "replace") {
                    payload = { name: attemptName, replace: true };
                    continue;
                }
                return;
            }
            console.error("Error renaming item:", error);
            let userMessage = "Failed to rename item.";
            const msg = (error && error.message) ? error.message : "";
            if (msg.includes("403") || msg.includes("permission")) {
                userMessage = "You don't have permission to rename this item.";
            } else if (msg.includes("404")) {
                userMessage = "The item no longer exists.";
            } else if (msg) {
                userMessage = `Failed to rename: ${msg.split('\n')[0]}`;
            }
            showInfo(userMessage);
            return;
        }
    }
}


async function handleMoveFiles() {
    if (selectedCogIds.size === 0) return;

    // Capture the SOURCE folder before opening the picker. The picker
    // modal + post-success refresh runs through several async hops; if
    // anything (modal flow, hashchange race) perturbs breadcrumbTrail
    // mid-flight, reading it again later would silently land us at
    // root. Snapshot now, use later.
    const sourceParentId = breadcrumbTrail.length > 0
        ? breadcrumbTrail[breadcrumbTrail.length - 1].id
        : null;

    openFolderPicker(async (destinationId) => {
        // data-files-launch-bulletproof §2.2 — track per-item outcomes
        // so partial-failure surfaces as the warning toast + modal,
        // not a single concatenated error.
        const itemsArray = Array.from(selectedItems.values());
        const succeeded = [];
        const skipped = [];

        // move-to-root-phantom (#5): the folder picker returns null for "Home",
        // but the root view lists the tenant MASTER_FOLDER (see loadFiles' Home
        // mapping), not the no-parent level. Moving to a raw null dropped the
        // file into the no-parent level, where the root view never looked — it
        // vanished / left a phantom. Resolve null to the real root folder id so
        // a file moved to root lands where the root listing actually reads.
        const resolvedDestinationId = (destinationId == null)
            ? await getRootFolderId()
            : destinationId;

        for (const item of itemsArray) {
            let payload = { parent_id: resolvedDestinationId };
            let done = false;
            while (!done) {
                try {
                    await api.cogs.update(item.id, payload);
                    succeeded.push({ id: item.id, name: item.name });
                    done = true;
                } catch (e) {
                    if (isNameCollisionError(e)) {
                        const choice = await resolveCollision(e);
                        if (!choice) {
                            skipped.push({ id: item.id, name: item.name, reason: REASON_CODES.NAME_COLLISION });
                            done = true;
                            break;
                        }
                        if (choice.action === "rename") {
                            payload = { parent_id: resolvedDestinationId, name: choice.new_name };
                            continue;
                        }
                        if (choice.action === "replace") {
                            payload = { parent_id: resolvedDestinationId, replace: true };
                            continue;
                        }
                        skipped.push({ id: item.id, name: item.name, reason: REASON_CODES.NAME_COLLISION });
                        done = true;
                        break;
                    }
                    // Non-collision error — map to closed-enum reason.
                    const status = (e && (e.status || e.statusCode)) || 0;
                    const detail = (e && (e.detail || e.message)) || '';
                    const lower = String(detail).toLowerCase();
                    let reason = REASON_CODES.UNKNOWN;
                    if (status === 403) {
                        if (lower.includes('owner')) reason = REASON_CODES.NOT_OWNER;
                        else reason = REASON_CODES.NO_PERMISSION;
                    } else if (status === 404) reason = REASON_CODES.NOT_FOUND;
                    else if (status === 413) reason = REASON_CODES.QUOTA_EXCEEDED;
                    else if (lower.includes('circular')) reason = REASON_CODES.NO_PERMISSION;
                    skipped.push({ id: item.id, name: item.name, reason });
                    done = true;
                }
            }
        }

        if (succeeded.length > 0) {
            showInfo(`Moved ${succeeded.length} item${succeeded.length > 1 ? 's' : ''}.`);
        }
        if (skipped.length > 0) {
            showBulkSkipWarning({ verb: 'move', skippedItems: skipped });
        }

        selectedCogIds.clear();
        selectedItems.clear();
        // Reload the SOURCE folder we started in — not whatever
        // breadcrumbTrail says now (which may have been clobbered
        // by an intervening modal/hashchange).
        loadFiles(sourceParentId);
    });
}

// deleted-files-filter (#4): restore a single soft-deleted file. Calls
// POST /cogs/{id}/restore, then reloads the Deleted view (the restored file
// drops off it and reappears in the normal listing on the next live load).
async function handleRestoreFile(item) {
    if (!item || item.id == null) return;
    try {
        await api.cogs.restore(item.id);
        showInfo(`Restored "${item.name}".`);
        selectedCogIds.delete(item.id);
        selectedItems.delete(item.id);
        // Reload whichever view we're in. In the Deleted view this re-pulls
        // the deleted list (now minus this file); loadStorageUsage refreshes
        // the quota bar since a restore reclaims space.
        loadFiles(_currentParentId);
        try { loadStorageUsage(); } catch (_) {}
    } catch (e) {
        showError(`Could not restore "${item.name}": ${e?.message || e}`);
    }
}

async function handleCopyFiles() {
    if (selectedCogIds.size === 0) return;

    // Snapshot the source folder before the picker modal runs — see
    // handleMoveFiles for the rationale.
    const sourceParentId = breadcrumbTrail.length > 0
        ? breadcrumbTrail[breadcrumbTrail.length - 1].id
        : null;

    openFolderPicker(async (destinationId) => {
        // data-files-launch-bulletproof §2.2 — same per-item tracking
        // pattern as bulk-move; copy is owner-only on the source so
        // not_owner is a common skip reason.
        const itemsArray = Array.from(selectedItems.values());
        const succeeded = [];
        const skipped = [];

        for (const item of itemsArray) {
            let opts = {};
            let done = false;
            while (!done) {
                try {
                    await api.cogs.copy(item.id, destinationId, opts);
                    succeeded.push({ id: item.id, name: item.name });
                    done = true;
                } catch (e) {
                    if (isNameCollisionError(e)) {
                        const choice = await resolveCollision(e);
                        if (!choice) {
                            skipped.push({ id: item.id, name: item.name, reason: REASON_CODES.NAME_COLLISION });
                            done = true;
                            break;
                        }
                        if (choice.action === "rename") {
                            opts = { new_name: choice.new_name };
                            continue;
                        }
                        if (choice.action === "replace") {
                            opts = { replace: true };
                            continue;
                        }
                        skipped.push({ id: item.id, name: item.name, reason: REASON_CODES.NAME_COLLISION });
                        done = true;
                        break;
                    }
                    const status = (e && (e.status || e.statusCode)) || 0;
                    const detail = (e && (e.detail || e.message)) || '';
                    const lower = String(detail).toLowerCase();
                    let reason = REASON_CODES.UNKNOWN;
                    if (status === 403) {
                        if (lower.includes('owner')) reason = REASON_CODES.NOT_OWNER;
                        else reason = REASON_CODES.NO_PERMISSION;
                    } else if (status === 404) reason = REASON_CODES.NOT_FOUND;
                    else if (status === 413 || lower.includes('storage')) reason = REASON_CODES.QUOTA_EXCEEDED;
                    skipped.push({ id: item.id, name: item.name, reason });
                    done = true;
                }
            }
        }

        if (succeeded.length > 0) {
            showInfo(`Copied ${succeeded.length} item${succeeded.length > 1 ? 's' : ''}.`);
        }
        if (skipped.length > 0) {
            showBulkSkipWarning({ verb: 'copy', skippedItems: skipped });
        }

        selectedCogIds.clear();
        selectedItems.clear();
        loadFiles(sourceParentId);
    });
}

async function openDeleteConfirmModal() {
    if (selectedCogIds.size === 0) return;
    const modal = await loadModal('delete-confirm');
    if (modal) {
        const confirmButton = modal.querySelector('#confirm-delete-button');
        const cancelButton = modal.querySelector('#cancel-delete-button');
        const messageElement = modal.querySelector('#delete-confirm-message');

        // Update message to show count
        if (messageElement && selectedCogIds.size > 1) {
            messageElement.textContent = `Are you sure you want to delete ${selectedCogIds.size} items? This action cannot be undone.`;
        }

        confirmButton.onclick = async () => {
            // data-files-launch-bulletproof §2.2 — route bulk-delete through
            // the aggregator so partial-failure paths surface as a warning
            // toast + skipped-items modal instead of silent skip.
            const itemsArray = Array.from(selectedItems.values());
            const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
            const token = sessionStorage.getItem('accessToken');

            const { succeeded, skipped } = await runBulkAction({
                items: itemsArray,
                action: (item) =>
                    bulkActionFetch(`${apiUrl}/assets/${item.id}`, {
                        method: 'DELETE',
                        headers: { Authorization: `Bearer ${token}` },
                    }),
            });

            closeModal('delete-confirm-modal');
            const parentId = breadcrumbTrail.length > 0 ? breadcrumbTrail[breadcrumbTrail.length - 1].id : null;

            // Clear selection
            selectedCogIds.clear();
            selectedItems.clear();

            if (succeeded.length > 0) {
                showInfo(`Deleted ${succeeded.length} item${succeeded.length > 1 ? 's' : ''}.`);
            }
            if (skipped.length > 0) {
                showBulkSkipWarning({ verb: 'delete', skippedItems: skipped });
            }

            loadFiles(parentId);
            loadStorageUsage(); // Refresh storage bar after delete
        };

        cancelButton.onclick = () => closeModal('delete-confirm-modal');
    }
}

// --- Page Initialization and Event Listeners ---
// Data-area tab switcher. The Data page now hosts both the file browser
// (the historical "Files" tab) and the Tenant Registry (container images
// pushed to the tenant's isolated Harbor project). Separate sidebar
// entries for these made the nav crowded and hid the conceptual link —
// they're both "data the tenant owns on this platform".
//
// The tenant-registry module is lazy-imported on first activation so
// navigating straight to Files doesn't pay the Harbor-fetch cost.
// document-level listeners survive page remounts; guard their install once.
let _f2HandlerInstalled = false;
async function _activateDataTab(tabName) {
    const tabs = document.querySelectorAll('#data-tabs .data-tab');
    tabs.forEach(t => {
        const active = t.dataset.tab === tabName;
        t.classList.toggle('text-primary', active);
        t.classList.toggle('border-primary', active);
        t.classList.toggle('text-gray-500', !active);
        t.classList.toggle('border-transparent', !active);
        t.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    document.querySelectorAll('.data-panel').forEach(p => {
        p.classList.toggle('hidden', p.id !== `data-panel-${tabName}`);
    });
    // SPA remount: the panel HTML re-injects on each page visit, so the
    // "already initialized" marker lives on the panel element itself.
    if (tabName === 'tenant-registry') {
        const regPanel = document.getElementById('data-panel-tenant-registry');
        if (regPanel && regPanel.dataset.wired) return;
        if (regPanel) regPanel.dataset.wired = '1';
        try {
            const mod = await import('./tenant-registry.js');
            mod.initializeTenantRegistryPage();
        } catch (e) {
            console.error('Failed to initialize Tenant Registry tab:', e);
        }
    }
}

export function initializeFilesPageListeners() {
    // Event listeners are attached using delegation on the #page-content container,
    // which is guaranteed to exist.
    const pageContent = document.getElementById('page-content');
    if (!pageContent) return;

    // Wire the data-area tab bar. Honour ?tab=<name> so the old
    // #tenant-registry sidebar link (now redirected to this page)
    // lands on the right tab instead of defaulting to Files.
    document.querySelectorAll('#data-tabs .data-tab').forEach(btn => {
        btn.addEventListener('click', () => _activateDataTab(btn.dataset.tab));
    });
    const initialTab = new URLSearchParams(window.location.search).get('tab');
    if (initialTab === 'tenant-registry') {
        _activateDataTab('tenant-registry');
    }

    // Load storage usage
    loadStorageUsage();

    // Live refresh: poll the current folder + open preview so shares/revokes
    // from another user surface without a manual reload.
    startFilesPolling();

    // Initialize preview panel close button
    initPreviewListeners();

    // Preview panel tab switching
    document.querySelectorAll('.preview-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.preview-tab').forEach(t => {
                t.classList.remove('text-primary', 'border-b-2', 'border-primary');
                t.classList.add('text-gray-500');
            });
            tab.classList.remove('text-gray-500');
            tab.classList.add('text-primary', 'border-b-2', 'border-primary');

            const target = tab.dataset.tab;
            document.getElementById('preview-tab-content').classList.toggle('hidden', target !== 'preview');
            document.getElementById('permissions-tab-content').classList.toggle('hidden', target !== 'permissions');
            document.getElementById('details-tab-content')?.classList.toggle('hidden', target !== 'details');

            if (target === 'permissions' && _previewCogId) {
                loadPreviewPermissions(_previewCogId);
            }
            if (target === 'details') {
                // Re-render in case dimensions / duration were captured
                // since openPreview() first ran (image load is async).
                import('./file-preview.js').then(m => m.populateDetailsTab?.());
            }
        });
    });

    // Preview panel share/request buttons are now dynamic — set in loadPreviewPermissions()

    // Use a single delegated listener to handle all clicks within the files page.
    pageContent.onclick = (e) => {
        const target = e.target;

        // Button clicks
        if (target.id === 'make-folder-button' || target.id === 'make-folder-button-empty') openCreateFolderModal();
        if (target.id === 'upload-file-button' || target.id === 'upload-file-button-empty') openUploadFileModal();
        if (target.id === 'rename-selected-button' && selectedCogIds.size > 0) handleRenameFiles();
        if (target.id === 'move-selected-button' && selectedCogIds.size > 0) handleMoveFiles();
        if (target.id === 'copy-selected-button' && selectedCogIds.size > 0) handleCopyFiles();
        if (target.id === 'download-selected-button' && selectedCogIds.size > 0) handleDownload();
        if (target.id === 'share-selected-button' && selectedCogIds.size > 0) handleBulkShare();
        if (target.id === 'delete-selected-button' && selectedCogIds.size > 0) openDeleteConfirmModal();
        if (target.id === 'select-all-checkbox') handleSelectAll();
        if (target.closest('#clear-search-button')) handleClearSearch();

        // View mode toggle
        if (target.closest('#view-grid-btn')) { setViewMode('grid'); return; }
        if (target.closest('#view-list-btn')) { setViewMode('list'); return; }

        // deleted-files-filter (#4): toggle the Deleted view on/off.
        if (target.closest('#toggle-deleted-btn')) { setShowDeleted(!showDeleted); return; }

        // Breadcrumb navigation
        const breadcrumbLink = target.closest('#breadcrumb a');
        if (breadcrumbLink) {
            e.preventDefault();
            // Clear selection when navigating via breadcrumb
            selectedCogIds.clear();
            selectedItems.clear();

            const targetIdStr = breadcrumbLink.dataset.id;
            const targetId = targetIdStr === 'null' ? null : parseInt(targetIdStr);
            const partIndex = breadcrumbTrail.findIndex(p => p.id === targetId);

            breadcrumbTrail = (targetId === null) ? [] : breadcrumbTrail.slice(0, partIndex + 1);
            loadFiles(targetId);
        }

        // Up button
        if(target.closest('#up-button')) {
            if (breadcrumbTrail.length > 0) {
                // Clear selection when navigating up
                selectedCogIds.clear();
                selectedItems.clear();

                breadcrumbTrail.pop();
                const parentId = breadcrumbTrail.length > 0 ? breadcrumbTrail[breadcrumbTrail.length - 1].id : null;
                loadFiles(parentId);
            }
        }
    };

    // Search input handler
    const searchInput = document.getElementById('search-files');
    const clearSearchButton = document.getElementById('clear-search-button');

    if (searchInput) {
        searchInput.addEventListener('input', (e) => {
            searchQuery = e.target.value;

            // Show/hide clear button based on input
            if (clearSearchButton) {
                if (searchQuery.trim() === '') {
                    clearSearchButton.classList.add('hidden');
                } else {
                    clearSearchButton.classList.remove('hidden');
                }
            }

            // Reload current folder with search filter
            const parentId = breadcrumbTrail.length > 0
                ? breadcrumbTrail[breadcrumbTrail.length - 1].id
                : null;
            loadFiles(parentId);
        });
    }

    // P3 — F2 keyboard shortcut for rename. Only fires when the files
    // page is active (pageContent is visible) and the focus isn't in a
    // form input (so users typing in search / new-folder dialogs aren't
    // interrupted).
    // document persists across SPA page visits, so keep a module-level
    // once-flag here (an element marker would stack one handler per visit).
    if (_f2HandlerInstalled) return;
    _f2HandlerInstalled = true;
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'F2') return;
        const tag = (e.target && e.target.tagName || '').toUpperCase();
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        if (!document.body.contains(pageContent)) return;
        if (selectedCogIds.size === 0) return;
        e.preventDefault();
        handleRenameFiles();
    });

    // Load initial files for the root directory.
    loadFiles(null);
}

// --- Storage Usage ---
function _formatBytes(bytes) {
    if (!bytes) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB'];
    // Clamp the unit index to the table — Math.log of an exabyte-ish
    // value lands at i=5+ and used to print "undefined" when the table
    // only went up to TB. Defensive against any caller that hands us
    // a sentinel-large number.
    let i = Math.floor(Math.log(bytes) / Math.log(k));
    if (i < 0) i = 0;
    if (i >= sizes.length) i = sizes.length - 1;
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
}

async function loadStorageUsage() {
    // 2400LR-cdu: /tenant/storage-breakdown denied on CE (manifest:1327 routers/tenant/*.py);
    // fail-open on unknown edition.
    const isCe = await api.isPaidEdition().then((paid) => !paid);
    if (isCe) {
        const container = document.getElementById('storage-usage-container');
        if (container) container.classList.add('hidden');
        document.getElementById('storage-breakdown-panel')?.classList.add('hidden');
        return;
    }
    const storageBar = document.getElementById('storage-usage-bar');
    const storageText = document.getElementById('storage-usage-text');
    if (!storageBar || !storageText) return;

    try {
        const data = await api.fetchApi('/tenant/storage-breakdown');
        const usedBytes = data.used_bytes || 0;
        // limit_bytes === null is the "unlimited" sentinel (platform admin).
        const isUnlimited = data.limit_bytes === null || data.limit_bytes === undefined;
        const limitBytes = isUnlimited ? 0 : (data.limit_bytes || 0);
        const percentage = (!isUnlimited && limitBytes > 0) ? Math.min((usedBytes / limitBytes) * 100, 100) : 0;

        storageText.textContent = isUnlimited
            ? `${_formatBytes(usedBytes)} used (unlimited)`
            : `${_formatBytes(usedBytes)} of ${_formatBytes(limitBytes)} used`;

        // Render the stacked bar — one <div> per bucket sized by its share
        // of the LIMIT (not of used), so empty space shows the remaining
        // quota visually. Matches the Pixel-style storage view.
        const barRoot = document.getElementById('storage-breakdown-bar');
        if (barRoot && data.buckets) {
            barRoot.innerHTML = '';
            (data.buckets || []).forEach((b) => {
                if (!b.bytes) return;
                const seg = document.createElement('div');
                const pct = limitBytes > 0 ? (b.bytes / limitBytes) * 100 : 0;
                seg.style.width = `${Math.min(pct, 100)}%`;
                seg.style.backgroundColor = b.color || '#6366f1';
                seg.className = 'h-2 transition-all duration-300';
                seg.title = `${b.label}: ${_formatBytes(b.bytes)}`;
                barRoot.appendChild(seg);
            });
            // Add grey tail for unused quota so the bar fills 100% of the container.
            const usedPct = limitBytes > 0 ? Math.min((usedBytes / limitBytes) * 100, 100) : 0;
            if (usedPct < 100) {
                const tail = document.createElement('div');
                tail.style.width = `${100 - usedPct}%`;
                tail.className = 'h-2 bg-gray-200';
                barRoot.appendChild(tail);
            }
            // Tint the meta row when over threshold.
            storageText.classList.remove('text-red-600', 'text-yellow-600', 'text-gray-600');
            if (percentage >= 90) storageText.classList.add('text-red-600');
            else if (percentage >= 75) storageText.classList.add('text-yellow-600');
            else storageText.classList.add('text-gray-600');
        }

        // Breakdown panel body — rendered regardless, toggled by the button.
        _renderStorageBreakdownPanel(data);
    } catch (error) {
        console.error('Error loading storage usage:', error);
        storageText.textContent = 'Unable to load';
    }
}

function _renderStorageBreakdownPanel(data) {
    const legend = document.getElementById('storage-breakdown-legend');
    const buckets = document.getElementById('storage-breakdown-buckets');
    if (!legend || !buckets) return;

    // Legend = color dot + label + bytes for each bucket.
    legend.innerHTML = (data.buckets || []).map((b) => `
        <span class="flex items-center gap-1.5">
            <span class="w-3 h-3 rounded-sm" style="background:${b.color || '#6366f1'}"></span>
            <span class="font-medium text-gray-700">${b.label}</span>
            <span class="text-gray-500">${_formatBytes(b.bytes)} · ${b.count} item${b.count === 1 ? '' : 's'}</span>
        </span>
    `).join('');

    // Per-bucket cards with help text + top consumers + per-item
    // "Take me there" link that sets the relevant page's search to the
    // item's name so the user lands on the exact thing.
    buckets.innerHTML = (data.buckets || []).map((b) => {
        const topList = (b.top || []).length === 0
            ? `<div class="text-xs text-gray-400 italic">No items yet.</div>`
            : `<ul class="space-y-1">${(b.top || []).map((item) => {
                    const safeName = (item.name || '').replace(/"/g, '&quot;');
                    const title = (item.image_ref || item.name || '').replace(/"/g, '&quot;');
                    return `
                    <li class="flex items-center justify-between gap-2 text-xs">
                        <span class="truncate text-gray-700 flex-1 min-w-0" title="${title}">${item.name || '(unnamed)'}</span>
                        <span class="text-gray-500 tabular-nums flex-shrink-0">${_formatBytes(item.size_bytes)}</span>
                        <button
                            class="storage-takemethere text-primary hover:text-primary/80 underline flex-shrink-0"
                            data-bucket="${b.key}"
                            data-name="${safeName}"
                        >Take me there</button>
                    </li>
                `;
                }).join('')}</ul>`;
        return `
            <div class="border border-gray-200 rounded-lg p-3">
                <div class="flex items-center justify-between mb-1">
                    <div class="flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full" style="background:${b.color || '#6366f1'}"></span>
                        <span class="text-sm font-semibold text-gray-800">${b.label}</span>
                    </div>
                    <span class="text-xs text-gray-500">${_formatBytes(b.bytes)} · ${b.count} item${b.count === 1 ? '' : 's'}</span>
                </div>
                <p class="text-xs text-gray-500 mb-2">${b.help || ''}</p>
                ${topList}
            </div>
        `;
    }).join('');
}

// Event delegation for the storage breakdown toggle + recalculate
// button. The Dashboard shell injects pages dynamically, so the button
// DOM may not exist when files.js first loads; direct addEventListener
// would miss the element. Delegating off document.body is timing-
// independent and the guard (dataset.wired) makes it idempotent across
// page navigations.
if (!document.body.dataset.storageDelegationWired) {
    document.body.dataset.storageDelegationWired = '1';
    document.body.addEventListener('click', async (e) => {
        const target = e.target;
        if (!target || !target.closest) return;

        const toggle = target.closest('#storage-help-toggle');
        if (toggle) {
            e.preventDefault();
            const panel = document.getElementById('storage-breakdown-panel');
            if (panel) {
                panel.classList.toggle('hidden');
                toggle.textContent = panel.classList.contains('hidden')
                    ? "What's using my storage?"
                    : "Hide storage breakdown";
            }
            return;
        }

        const reconcile = target.closest('#storage-reconcile-btn');
        if (reconcile) {
            e.preventDefault();
            // 2400LR-cdu: /tenant/storage/reconcile denied on CE (manifest:1327 routers/tenant/*.py);
            // fail-open on unknown edition.
            if (await api.isPaidEdition().then((paid) => !paid)) return;
            const original = reconcile.textContent;
            reconcile.textContent = 'Recalculating…';
            try {
                await api.fetchApi('/tenant/storage/reconcile', { method: 'POST' });
                await loadStorageUsage();
                reconcile.textContent = original || 'Recalculate';
            } catch (err) {
                console.error('Storage reconcile failed:', err);
                reconcile.textContent = 'Retry';
            }
            return;
        }

        const takeMeThere = target.closest('.storage-takemethere');
        if (takeMeThere) {
            e.preventDefault();
            const bucket = takeMeThere.dataset.bucket;
            const name = takeMeThere.dataset.name || '';
            if (bucket === 'node_images') {
                // Cross-app jump to Workflow Builder with the node name
                // pre-filled in the library search. The builder reads
                // ?node_search=<name> on mount. Needs the auth token
                // too so the session carries over.
                // GSEC R-074: tokens in the FRAGMENT (never sent to the
                // server / logged); node_search stays a query param.
                const token = sessionStorage.getItem('accessToken') || '';
                const refresh = sessionStorage.getItem('refreshToken') || '';
                const base = window.APP_CONFIG?.WORKFLOWS_URL || 'http://workflows.localhost';
                const params = new URLSearchParams({ node_search: name });
                const fragParts = [];
                if (token) fragParts.push(`token=${encodeURIComponent(token)}`);
                if (refresh) fragParts.push(`refresh_token=${encodeURIComponent(refresh)}`);
                window.location.href = `${base}?${params.toString()}${fragParts.length ? `#${fragParts.join('&')}` : ''}`;
            } else if (bucket === 'files' || bucket === 'workflow_outputs') {
                // Already on the Data page — just populate the search
                // box and dispatch an input event so the existing
                // filter logic reacts.
                const searchInput = document.getElementById('search-files');
                if (searchInput) {
                    searchInput.value = name;
                    searchInput.dispatchEvent(new Event('input', { bubbles: true }));
                    searchInput.focus();
                    // Scroll the file list into view (we're still next to
                    // the storage card at the top of the page).
                    const list = document.querySelector('#file-list, .file-list, [id*="file-list"]');
                    if (list) list.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
                // Collapse the storage panel so the list has the user's focus.
                const panel = document.getElementById('storage-breakdown-panel');
                const toggle = document.getElementById('storage-help-toggle');
                if (panel && !panel.classList.contains('hidden')) {
                    panel.classList.add('hidden');
                    if (toggle) toggle.textContent = "What's using my storage?";
                }
            }
            return;
        }
    });
}

// --- Search Functions ---
function handleClearSearch() {
    const searchInput = document.getElementById('search-files');
    const clearSearchButton = document.getElementById('clear-search-button');

    if (searchInput) {
        searchInput.value = '';
        searchQuery = '';
    }

    if (clearSearchButton) {
        clearSearchButton.classList.add('hidden');
    }

    // Reload current folder without filter
    const parentId = breadcrumbTrail.length > 0
        ? breadcrumbTrail[breadcrumbTrail.length - 1].id
        : null;
    loadFiles(parentId);
}
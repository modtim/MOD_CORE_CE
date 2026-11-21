// Dashboard/js/file-preview.js
//
// Asset preview panel for the files page.
// Handles image, 3D model (via Three.js CDN), video, and generic file previews.

import { tags as tagsApi, locks as locksApi, me as meApi } from './api.js';
import { loadAssetMarkingBanner, clearAssetMarkingBanner } from './data-marking-banner.js';

const API_BASE_URL = window.APP_CONFIG?.API_URL || "http://api.localhost";

// target_type used when locking a cog resource via the /locks/ endpoints.
const LOCK_TARGET_TYPE = 'cog';

// Per-cog caches for the Details tab's Tags + Lock sections. populateDetailsTab
// runs synchronously (and repeatedly, as media onload callbacks fire), so it
// renders from these caches and only triggers a network refresh when the cache
// is stale for the current cog. `lock === undefined` means "not yet fetched";
// `null` means "confirmed unlocked".
let _tagsCache = { cogId: null, tags: null, loading: false };
let _lockCache = { cogId: null, lock: undefined, loading: false };
let _currentUserId = null;
// Whether the current user may force-unlock someone else's lock: platform
// admins, or a tenant "Admin" system-role holder. Mirrors the backend
// _can_force_unlock() rule in routers/locks.py.
let _currentUserCanForceUnlock = false;
// FX.163 p3b4a — tenant-admin / superadmin verdict for the banner's "Clear"
// button (same predicate as assistant-knowledge.js:51: platform admin OR
// tenant root, i.e. parent_user_id IS NULL). /me is coalesced+cached in api.js.
let _currentUserCanClearMarking = null;

// File extension categorization
const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'tif', 'tiff', 'tga', 'bmp', 'gif', 'webp']);
const HDR_EXTS = new Set(['exr', 'hdr']);
const MODEL3D_EXTS = new Set(['obj', 'fbx', 'glb', 'gltf', 'ply', 'stl', 'dae']);
const VIDEO_EXTS = new Set(['mp4', 'mkv', 'avi', 'mov', 'webm']);
const TEXT_EXTS = new Set([
    'json', 'csv', 'txt', 'yaml', 'yml', 'xml', 'toml', 'ini', 'cfg', 'conf', 'env', 'md', 'markdown',
    'py', 'sh', 'bash', 'js', 'ts', 'jsx', 'tsx', 'html', 'css', 'scss', 'less',
    'go', 'rs', 'rb', 'java', 'c', 'cpp', 'h', 'hpp', 'cs', 'php', 'pl', 'lua', 'r',
    'sql', 'graphql', 'proto', 'dockerfile', 'makefile', 'gitignore',
    'log', 'dat', 'nfo', 'readme',
]);
// Subset of image formats browsers can natively render
const BROWSER_IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg']);
// Subset of 3D formats Three.js can load easily via CDN loaders
const THREEJS_EXTS = new Set(['glb', 'gltf', 'obj', 'stl', 'ply', 'fbx']);
const USD_CONVERT_EXTS = new Set(['usd', 'usda', 'usdc', 'usdz']);  // Server-side GLB conversion

let threeLoaded = false;
let threeModules = {};
let currentPreviewCogId = null;
let activeRenderer = null;
let activeAnimationId = null;

// Latest opened cog + media measurements (filled in by IMG/VIDEO/AUDIO
// onload callbacks). The Details tab reads these so dimensions /
// duration show up even when Details was opened before the media
// finished loading.
let _currentItem = null;
let _mediaInfo = { width: null, height: null, duration_s: null };

function _resetMediaInfo() { _mediaInfo = { width: null, height: null, duration_s: null }; }

// Format a duration in seconds as "1h 23m 45s" / "23m 45s" / "45s".
function _formatDuration(s) {
    if (s == null || !Number.isFinite(s)) return null;
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = Math.floor(s % 60);
    if (h > 0) return `${h}h ${m}m ${sec}s`;
    if (m > 0) return `${m}m ${sec}s`;
    return `${sec}s`;
}

function _escapeHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Render the Details tab. Called from the tab switcher on click and
// also from openPreview() so the tab is preloaded with what we know
// before any IMG/VIDEO load fires.
export function populateDetailsTab() {
    const body = document.getElementById('details-tab-body');
    if (!body) return;
    const item = _currentItem;
    if (!item) {
        body.innerHTML = '<p class="text-sm text-gray-500">Select a file to see details.</p>';
        return;
    }
    const ext = resolveExt(item);
    const category = categorize(ext);
    const rows = [];
    const row = (label, value) => {
        if (value == null || value === '') return;
        rows.push(`
            <div class="flex justify-between items-baseline gap-3 py-1.5 border-b border-gray-100">
                <span class="text-gray-500 text-xs uppercase tracking-wide">${_escapeHtml(label)}</span>
                <span class="font-medium text-gray-800 text-sm text-right break-words">${_escapeHtml(value)}</span>
            </div>
        `);
    };

    row('Name', item.name);
    row('Type', (ext || item.cog_type || '').toUpperCase());
    row('Category', category === 'model3d' ? '3D Model' : (category ? category.charAt(0).toUpperCase() + category.slice(1) : null));
    row('MIME', item.mime_type || null);
    row('Size', item.size_bytes ? formatBytes(item.size_bytes) : null);
    if (_mediaInfo.width && _mediaInfo.height) {
        row('Dimensions', `${_mediaInfo.width} × ${_mediaInfo.height}`);
        const mp = (_mediaInfo.width * _mediaInfo.height / 1_000_000);
        if (mp >= 0.1) row('Megapixels', mp.toFixed(2));
    }
    if (_mediaInfo.duration_s != null) {
        row('Duration', _formatDuration(_mediaInfo.duration_s));
    }
    row('Created', item.created_at ? new Date(item.created_at).toLocaleString() : null);
    row('Modified', item.updated_at ? new Date(item.updated_at).toLocaleString() : null);
    row('Cog ID', item.id != null ? String(item.id) : null);
    if (item.content_hash) {
        row('Hash (SHA-256)', item.content_hash);
    }

    const detailRows = rows.length
        ? rows.join('')
        : '<p class="text-sm text-gray-500">No details available.</p>';

    body.innerHTML = detailRows + _renderTagsSection(item) + _renderLockSection(item);

    // Kick off (or reuse) the async tag/lock loads for this cog and wire up
    // the interactive controls that were just rendered.
    _ensureTagsLoaded(item.id);
    _ensureLockLoaded(item.id);
    _wireDetailsControls(item);
}

// -----------------------------------------------------------------------
// Details tab — Tags section
// -----------------------------------------------------------------------
function _renderTagsSection(item) {
    let inner;
    if (_tagsCache.cogId === item.id && Array.isArray(_tagsCache.tags)) {
        if (_tagsCache.tags.length === 0) {
            inner = '<p class="text-xs text-gray-400 italic">No tags yet.</p>';
        } else {
            inner = _tagsCache.tags.map((t) => `
                <span class="inline-flex items-center gap-1 px-2 py-0.5 bg-gray-100 text-gray-700 text-xs rounded-full">
                    ${_escapeHtml(t.name)}
                    <button type="button" class="details-tag-remove text-gray-400 hover:text-red-600 leading-none font-semibold"
                            data-tag-id="${t.id}" title="Remove tag" aria-label="Remove tag ${_escapeHtml(t.name)}">&times;</button>
                </span>
            `).join('');
        }
    } else {
        inner = '<p class="text-xs text-gray-400">Loading tags…</p>';
    }

    return `
        <div class="pt-3">
            <div class="text-gray-500 text-xs uppercase tracking-wide mb-2">Tags</div>
            <div id="details-tags-list" class="flex flex-wrap gap-1.5 mb-2">${inner}</div>
            <form id="details-tag-add-form" class="flex gap-1.5">
                <input id="details-tag-input" type="text" maxlength="64" placeholder="Add a tag…" aria-label="Add a tag"
                       class="flex-1 min-w-0 px-2 py-1 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-primary" />
                <button type="submit" class="px-3 py-1 text-sm bg-primary text-white rounded hover:opacity-90 flex-shrink-0">Add</button>
            </form>
            <div id="details-tag-error" class="text-xs text-red-600 mt-1 hidden"></div>
        </div>
    `;
}

// -----------------------------------------------------------------------
// Details tab — Lock section
// -----------------------------------------------------------------------
function _renderLockSection(item) {
    let inner;
    if (_lockCache.cogId !== item.id || _lockCache.lock === undefined) {
        inner = '<p class="text-xs text-gray-400">Checking lock status…</p>';
    } else if (_lockCache.lock === null) {
        inner = `
            <div class="flex items-center justify-between gap-2">
                <span class="inline-flex items-center gap-1.5 text-sm text-gray-700">
                    <span class="w-2 h-2 rounded-full bg-green-500"></span>Unlocked
                </span>
                <button type="button" id="details-lock-toggle" data-action="lock"
                        class="px-3 py-1 text-sm bg-primary text-white rounded hover:opacity-90">Lock file</button>
            </div>
            <input id="details-lock-reason" type="text" maxlength="500" placeholder="Reason (optional) — e.g. editing hero shot" aria-label="Lock reason"
                   class="mt-2 w-full px-2 py-1 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-primary" />
        `;
    } else {
        const lock = _lockCache.lock;
        const heldByMe = _currentUserId != null && lock.user_id === _currentUserId;
        // A user lock now holds until explicitly unlocked: expires_at is null in
        // the default no-expiry case. Only show a countdown / Renew for a lock
        // that actually carries an expiry (opt-in timed lock).
        const isTimed = !!lock.expires_at;
        const expires = isTimed ? new Date(lock.expires_at).toLocaleString() : null;
        const holderName = lock.locked_by_username || lock.locked_by_email;
        const holder = heldByMe
            ? 'you'
            : (holderName ? _escapeHtml(String(holderName)) : `another user (#${_escapeHtml(String(lock.user_id))})`);
        const reason = lock.reason ? _escapeHtml(String(lock.reason)) : null;
        // Held-by-me controls: Unlock always; Renew ONLY for a timed lock (a
        // no-expiry lock never needs renewing — no surprise timeout).
        const myControls = isTimed
            ? `<button type="button" id="details-lock-renew" data-action="renew"
                     class="px-3 py-1 text-sm bg-primary text-white rounded hover:opacity-90">Renew</button>
               <button type="button" id="details-lock-toggle" data-action="unlock"
                     class="px-3 py-1 text-sm bg-gray-200 text-gray-800 rounded hover:bg-gray-300">Unlock</button>`
            : `<button type="button" id="details-lock-toggle" data-action="unlock"
                     class="px-3 py-1 text-sm bg-gray-200 text-gray-800 rounded hover:bg-gray-300">Unlock</button>`;
        const label = (heldByMe && !isTimed)
            ? 'Locked by you — held until you unlock'
            : `Locked by ${holder}`;
        inner = `
            <div class="flex items-center justify-between gap-2">
                <span class="inline-flex items-center gap-1.5 text-sm text-gray-700">
                    <span class="w-2 h-2 rounded-full bg-amber-500"></span>${label}
                </span>
                <span class="flex items-center gap-1.5 flex-shrink-0">
                    ${heldByMe
                        ? myControls
                        : (_currentUserCanForceUnlock
                            ? `<button type="button" id="details-lock-force" data-action="force"
                                     class="px-3 py-1 text-sm bg-red-600 text-white rounded hover:bg-red-700">Force unlock</button>`
                            : '')}
                </span>
            </div>
            ${reason ? `<p class="text-xs text-gray-600 mt-1">Reason: ${reason}</p>` : ''}
            ${expires ? `<p class="text-xs text-gray-400 mt-1">Expires ${_escapeHtml(expires)}</p>` : ''}
            ${(heldByMe && !isTimed) ? '<p class="text-xs text-gray-400 mt-1">This lock stays until you unlock it.</p>' : ''}
            ${(!heldByMe && !_currentUserCanForceUnlock) ? '<p class="text-xs text-gray-400 mt-1">Only the lock holder or an admin can unlock this file.</p>' : ''}
            ${(!heldByMe && _currentUserCanForceUnlock) ? '<p class="text-xs text-gray-400 mt-1">Force-unlock is audited as a security override.</p>' : ''}
        `;
    }

    return `
        <div class="pt-3">
            <div class="text-gray-500 text-xs uppercase tracking-wide mb-2">Lock</div>
            <div id="details-lock-body">${inner}</div>
            <div id="details-lock-error" class="text-xs text-red-600 mt-1 hidden"></div>
        </div>
    `;
}

// Attach event listeners to the controls rendered by the tag/lock sections.
// Called after every populateDetailsTab() innerHTML swap.
function _wireDetailsControls(item) {
    const addForm = document.getElementById('details-tag-add-form');
    if (addForm) {
        addForm.addEventListener('submit', (e) => {
            e.preventDefault();
            const input = document.getElementById('details-tag-input');
            const name = (input?.value || '').trim();
            if (name) _addTag(item.id, name);
        });
    }
    document.querySelectorAll('.details-tag-remove').forEach((btn) => {
        btn.addEventListener('click', () => {
            const tagId = Number(btn.getAttribute('data-tag-id'));
            if (tagId) _removeTag(item.id, tagId);
        });
    });
    const lockToggle = document.getElementById('details-lock-toggle');
    if (lockToggle) {
        lockToggle.addEventListener('click', () => {
            _toggleLock(item.id, lockToggle.getAttribute('data-action') === 'lock');
        });
    }
    const lockRenew = document.getElementById('details-lock-renew');
    if (lockRenew) {
        lockRenew.addEventListener('click', () => _renewLock(item.id));
    }
    const lockForce = document.getElementById('details-lock-force');
    if (lockForce) {
        lockForce.addEventListener('click', () => _forceUnlock(item.id));
    }
}

function _showDetailsError(elId, msg) {
    const el = document.getElementById(elId);
    if (!el) return;
    el.textContent = msg;
    el.classList.remove('hidden');
}

// -----------------------------------------------------------------------
// Details tab — async data loaders + mutations
// -----------------------------------------------------------------------
function _ensureTagsLoaded(cogId) {
    if (_tagsCache.cogId === cogId && _tagsCache.tags !== null) return;
    if (_tagsCache.loading && _tagsCache.cogId === cogId) return;
    _refreshTags(cogId);
}

async function _refreshTags(cogId) {
    _tagsCache = { cogId, tags: _tagsCache.cogId === cogId ? _tagsCache.tags : null, loading: true };
    try {
        const list = await tagsApi.listForCog(cogId);
        _tagsCache = { cogId, tags: Array.isArray(list) ? list : [], loading: false };
    } catch (e) {
        _tagsCache = { cogId, tags: [], loading: false };
        console.warn('Failed to load tags:', e);
    }
    // Only repaint if the user is still looking at this cog.
    if (_currentItem && _currentItem.id === cogId) populateDetailsTab();
}

function _ensureLockLoaded(cogId) {
    if (_lockCache.cogId === cogId && _lockCache.lock !== undefined) return;
    if (_lockCache.loading && _lockCache.cogId === cogId) return;
    _refreshLock(cogId);
}

// FX.163 p3b4a — tenant-admin / superadmin verdict for the banner's "Clear"
// button. Cached once per session (api.js coalesces /me), silent → false.
async function _canClearMarking() {
    if (_currentUserCanClearMarking === null) {
        try {
            const me = await meApi.get();
            _currentUserCanClearMarking = !!me && (me.is_platform_admin === true || me.parent_user_id === null);
        } catch {
            return false;
        }
    }
    return _currentUserCanClearMarking;
}

async function _refreshLock(cogId) {
    _lockCache = { cogId, lock: _lockCache.cogId === cogId ? _lockCache.lock : undefined, loading: true };
    try {
        if (_currentUserId == null) {
            try {
                const me = await meApi.get();
                _currentUserId = me?.id ?? null;
                _currentUserCanForceUnlock = !!(me && (me.is_platform_admin || me.custom_role?.name === 'Admin'));
            } catch { /* non-fatal */ }
        }
        const lock = await locksApi.getStatus(LOCK_TARGET_TYPE, cogId);
        _lockCache = { cogId, lock: lock || null, loading: false };
    } catch (e) {
        _lockCache = { cogId, lock: null, loading: false };
        console.warn('Failed to load lock status:', e);
    }
    if (_currentItem && _currentItem.id === cogId) populateDetailsTab();
}

// Create-or-find the tag by name, attach it to the cog, then refresh.
async function _addTag(cogId, name) {
    const input = document.getElementById('details-tag-input');
    const errEl = document.getElementById('details-tag-error');
    if (errEl) errEl.classList.add('hidden');
    try {
        let tag;
        try {
            tag = await tagsApi.create(name);
        } catch (e) {
            if (e && e.status === 409) {
                const all = await tagsApi.listAll();
                tag = (all || []).find((t) => t.name.toLowerCase() === name.toLowerCase());
                if (!tag) throw e;
            } else {
                throw e;
            }
        }
        try {
            await tagsApi.attach(cogId, tag.id);
        } catch (e) {
            // 409 = already attached to this cog; treat as success.
            if (!(e && e.status === 409)) throw e;
        }
        if (input) input.value = '';
        await _refreshTags(cogId);
    } catch (e) {
        _showDetailsError('details-tag-error', e?.message || 'Failed to add tag.');
    }
}

async function _removeTag(cogId, tagId) {
    const errEl = document.getElementById('details-tag-error');
    if (errEl) errEl.classList.add('hidden');
    try {
        await tagsApi.detach(cogId, tagId);
        await _refreshTags(cogId);
    } catch (e) {
        _showDetailsError('details-tag-error', e?.message || 'Failed to remove tag.');
    }
}

async function _toggleLock(cogId, wantLock) {
    const errEl = document.getElementById('details-lock-error');
    if (errEl) errEl.classList.add('hidden');
    const btn = document.getElementById('details-lock-toggle');
    if (btn) { btn.disabled = true; btn.classList.add('opacity-50'); }
    try {
        if (wantLock) {
            const reasonEl = document.getElementById('details-lock-reason');
            const reason = (reasonEl?.value || '').trim() || null;
            await locksApi.acquire(LOCK_TARGET_TYPE, cogId, reason);
        } else {
            await locksApi.release(LOCK_TARGET_TYPE, cogId);
        }
        await _refreshLock(cogId);
    } catch (e) {
        _showDetailsError('details-lock-error', e?.message || (wantLock ? 'Failed to lock file.' : 'Failed to unlock file.'));
        // Re-sync actual state (e.g. someone else grabbed the lock → 409).
        await _refreshLock(cogId);
    }
}

async function _renewLock(cogId) {
    const errEl = document.getElementById('details-lock-error');
    if (errEl) errEl.classList.add('hidden');
    const btn = document.getElementById('details-lock-renew');
    if (btn) { btn.disabled = true; btn.classList.add('opacity-50'); }
    try {
        await locksApi.renew(LOCK_TARGET_TYPE, cogId);
        await _refreshLock(cogId);
    } catch (e) {
        _showDetailsError('details-lock-error', e?.message || 'Failed to renew lock.');
        await _refreshLock(cogId);
    }
}

async function _forceUnlock(cogId) {
    const errEl = document.getElementById('details-lock-error');
    if (errEl) errEl.classList.add('hidden');
    if (!window.confirm('Force-unlock this file? The current holder will lose their lock. This action is audited.')) {
        return;
    }
    const btn = document.getElementById('details-lock-force');
    if (btn) { btn.disabled = true; btn.classList.add('opacity-50'); }
    try {
        await locksApi.forceRelease(LOCK_TARGET_TYPE, cogId);
        await _refreshLock(cogId);
    } catch (e) {
        _showDetailsError('details-lock-error', e?.message || 'Failed to force-unlock file.');
        await _refreshLock(cogId);
    }
}

function getExtension(filename) {
    if (!filename) return '';
    const dot = filename.lastIndexOf('.');
    return dot >= 0 ? filename.substring(dot + 1).toLowerCase() : '';
}

// Canonical "what kind of file is this" lookup for a cog item. Prefers
// the backend-supplied authoritative metadata (`file_extension` from
// ContentBlob, set at upload time and never mutated by rename) and only
// falls back to parsing the display name when the API didn't surface it
// (legacy cogs without a blob row, or cog payloads from older endpoints
// that haven't been re-fetched). Renaming a clip.mp4 to "demo" with no
// extension MUST still pick the video visualizer — that's what this
// helper guarantees.
const MIME_EXT_MAP = {
    'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif',
    'image/webp': 'webp', 'image/bmp': 'bmp', 'image/tiff': 'tiff',
    'image/svg+xml': 'svg', 'image/x-exr': 'exr',
    'video/mp4': 'mp4', 'video/quicktime': 'mov', 'video/webm': 'webm',
    'video/x-msvideo': 'avi', 'video/x-matroska': 'mkv',
    'audio/mpeg': 'mp3', 'audio/wav': 'wav', 'audio/x-wav': 'wav',
    'audio/flac': 'flac', 'audio/ogg': 'ogg',
    'application/pdf': 'pdf',
    'model/gltf-binary': 'glb', 'model/gltf+json': 'gltf',
    'model/obj': 'obj', 'model/stl': 'stl',
    'application/json': 'json', 'text/csv': 'csv', 'text/plain': 'txt',
    'application/zip': 'zip', 'application/x-tar': 'tar',
    'application/gzip': 'gz',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'application/msword': 'doc',
};

function resolveExt(item) {
    if (!item) return '';
    if (item.file_extension) return String(item.file_extension).toLowerCase();
    if (item.mime_type && MIME_EXT_MAP[item.mime_type]) return MIME_EXT_MAP[item.mime_type];
    return getExtension(item.name);
}

const PDF_EXTS = new Set(['pdf']);
const DOCX_EXTS = new Set(['docx', 'doc']);
const SPLAT_EXTS = new Set(['splat', 'ksplat']);
// .e57 added (#7, launch-day-followup-2 §5.6) — same Three.js
// renderer handles the dispatch; the e57 parser inside
// renderPointCloudPreview falls back to a "no preview available"
// panel if the in-browser binary reader can't decode the file.
const POINTCLOUD_EXTS = new Set(['pcd', 'las', 'laz', 'xyz', 'e57']);
const AUDIO_EXTS = new Set(['mp3', 'wav', 'flac', 'ogg', 'aac', 'm4a', 'wma']);
// CAD formats that need a triangulator before they can render in
// Three.js. STL/OBJ are NOT here — they already render via the
// 'model3d' path. BREP is included so it's caught by the CAD panel
// instead of falling through to "other".
// `.sldprt` lives here too — it routes through `renderCADPreview` which
// detects the SolidWorks extension and fetches the server-side
// converted STEP via /cogs/{id}/converted-step instead of
// sending the raw .sldprt bytes to occt-import-js (which can't read it).
// (#8 / launch-day-followup-2 §5.8).
const CAD_EXTS = new Set(['step', 'stp', 'iges', 'igs', 'brep', 'sldprt']);
const FONT_EXTS = new Set(['ttf', 'otf', 'woff', 'woff2']);
const ARCHIVE_EXTS = new Set(['zip', 'tar', 'gz', 'tgz', 'rar', '7z', 'bz2']);

function categorize(ext) {
    if (IMAGE_EXTS.has(ext)) return 'image';
    if (USD_CONVERT_EXTS.has(ext)) return 'usd';
    if (MODEL3D_EXTS.has(ext)) return 'model3d';
    if (VIDEO_EXTS.has(ext)) return 'video';
    if (TEXT_EXTS.has(ext)) return 'text';
    if (PDF_EXTS.has(ext)) return 'pdf';
    if (DOCX_EXTS.has(ext)) return 'docx';
    if (SPLAT_EXTS.has(ext)) return 'splat';
    if (POINTCLOUD_EXTS.has(ext)) return 'pointcloud';
    if (AUDIO_EXTS.has(ext)) return 'audio';
    if (CAD_EXTS.has(ext)) return 'cad';
    if (HDR_EXTS.has(ext)) return 'hdr';
    if (FONT_EXTS.has(ext)) return 'font';
    if (ARCHIVE_EXTS.has(ext)) return 'archive';
    return 'other';
}

function formatBytes(bytes) {
    if (bytes == null) return '—';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

// -----------------------------------------------------------------------
// Three.js lazy loader (loaded from CDN on first 3D preview)
// -----------------------------------------------------------------------
async function ensureThreeLoaded() {
    if (threeLoaded) return;

    // Load Three.js core + addons via dynamic import from the vendored
    // local copy (see /vendor/three@0.170.0 — mirrors the index.html
    // importmap so this resolves offline/air-gapped).
    const THREE = await import('/vendor/three@0.170.0/build/three.module.min.js');
    const { OrbitControls } = await import('/vendor/three@0.170.0/examples/jsm/controls/OrbitControls.js');
    const { GLTFLoader } = await import('/vendor/three@0.170.0/examples/jsm/loaders/GLTFLoader.js');
    const { OBJLoader } = await import('/vendor/three@0.170.0/examples/jsm/loaders/OBJLoader.js');
    const { STLLoader } = await import('/vendor/three@0.170.0/examples/jsm/loaders/STLLoader.js');
    const { PLYLoader } = await import('/vendor/three@0.170.0/examples/jsm/loaders/PLYLoader.js');
    const { FBXLoader } = await import('/vendor/three@0.170.0/examples/jsm/loaders/FBXLoader.js');
    const { USDZLoader } = await import('/vendor/three@0.170.0/examples/jsm/loaders/USDZLoader.js');

    threeModules = { THREE, OrbitControls, GLTFLoader, OBJLoader, STLLoader, PLYLoader, FBXLoader, USDZLoader };
    threeLoaded = true;
}

// -----------------------------------------------------------------------
// Fetch presigned download URL for a cog
// -----------------------------------------------------------------------
async function getPresignedUrl(cogId) {
    const token = sessionStorage.getItem("accessToken");
    const resp = await fetch(`${API_BASE_URL}/assets/${cogId}/download`, {
        headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
    });
    if (!resp.ok) throw new Error(`Failed to get download URL: ${resp.status}`);
    const data = await resp.json();
    return data.download_url;
}

// -----------------------------------------------------------------------
// Same-origin authorised stream URL for a cog's bytes.
// Reuses the EXISTING tenant-scoped stream route (stream_file) — never a
// presigned MinIO host. Auth is the ?token= QUERY (mirrors the PDF path):
// the loaders cannot set an Authorization header on their own fetch.
// -----------------------------------------------------------------------
function getStreamUrl(cogId) {
    const token = sessionStorage.getItem("accessToken");
    return `${API_BASE_URL}/assets/${cogId}/stream?token=${encodeURIComponent(token)}`;
}

// Legacy URL-based load (the loader fetches the URL it is handed) — kept for the
// USDZ-converted GLB, PLY/splat and point-cloud callers, out of this job's scope.
function loadModelFromUrl(loader, url) {
    return new Promise((resolve, reject) => {
        loader.load(url, resolve, undefined, reject);
    });
}

// Parse model bytes with the right loader convention: GLTFLoader.parse is
// callback-based (a text .gltf with external .bin/.png resources cannot resolve
// them through this route — such a file falls back to the no-preview panel);
// OBJ/STL/PLY/FBX loaders parse synchronously (OBJ takes decoded text).
function parseModelBytes(loader, ext, bytes) {
    if (ext === 'obj') return loader.parse(new TextDecoder().decode(bytes));
    if (ext === 'stl' || ext === 'ply' || ext === 'fbx') return loader.parse(bytes);
    return new Promise((resolve, reject) => { loader.parse(bytes, '', resolve, reject); });
}

// -----------------------------------------------------------------------
// Cleanup previous preview renderer
// -----------------------------------------------------------------------
function cleanupRenderer() {
    if (activeAnimationId) {
        cancelAnimationFrame(activeAnimationId);
        activeAnimationId = null;
    }
    if (activeRenderer) {
        activeRenderer.dispose();
        activeRenderer = null;
    }
    // Stop any image-sequence playback that was running before we
    // swap in a new preview, so the timer doesn't keep flipping
    // frames into a torn-down container.
    cleanupSequencePlayer();
}

// -----------------------------------------------------------------------
// USD/USDZ preview — server-side GLB conversion rendered inline via Three.js
// -----------------------------------------------------------------------
async function renderUSDPreview(container, url, ext, cogId, _retryCount) {
    const retryCount = _retryCount || 0;
    const maxRetries = 120; // 120 x 1s = 2 minutes max wait

    if (!cogId) {
        container.innerHTML = `<div class="text-center text-gray-400 py-8 text-sm">Preview not available</div>`;
        return;
    }

    if (retryCount === 0) {
        container.innerHTML = `<div class="text-center text-gray-400 py-8 text-sm animate-pulse">Loading 3D preview...</div>`;
    }

    const token = sessionStorage.getItem("accessToken");

    try {
        const resp = await fetch(`${API_BASE_URL}/assets/${cogId}/thumbnail`, {
            headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
        });

        if (resp.ok) {
            const data = await resp.json();

            // GLB preview available — render interactive 3D inline
            if (data.glb_url) {
                container.innerHTML = '';
                await render3DPreview(container, data.glb_url, 'glb', cogId, true);
                return;
            }

            // PNG thumbnail only — show static image
            if (data.url) {
                container.innerHTML = '';
                renderImagePreview(container, data.url, `${ext} preview`, cogId);
                return;
            }
        }

        // No preview yet — show generating message with countdown
        if (retryCount < maxRetries) {
            const remaining = (maxRetries - retryCount) * 5;
            container.innerHTML = `<div class="text-center py-8">
                <div class="text-4xl mb-2">🎲</div>
                <div class="text-sm font-medium text-gray-700 mb-1">${ext.toUpperCase()} 3D File</div>
                <div class="text-xs text-gray-400 animate-pulse">Generating 3D preview...</div>
            </div>`;

            // Poll every 5 seconds — check if container is still in DOM
            setTimeout(() => {
                if (container.isConnected) {
                    renderUSDPreview(container, url, ext, cogId, retryCount + 1);
                }
            }, 1000);
        } else {
            container.innerHTML = `<div class="text-center py-8">
                <div class="text-4xl mb-2">🎲</div>
                <div class="text-sm font-medium text-gray-700 mb-1">${ext.toUpperCase()} 3D File</div>
                <div class="text-xs text-gray-400">Preview generation may still be in progress. Click the file again to check.</div>
            </div>`;
        }

    } catch (e) {
        container.innerHTML = `<div class="text-center text-gray-400 py-8 text-sm">Preview not available</div>`;
    }
}

// -----------------------------------------------------------------------
// 3D Model preview (GLB, OBJ, STL, PLY, FBX via Three.js)
// -----------------------------------------------------------------------
async function render3DPreview(container, url, ext, cogId, boostLighting, useStream = false) {
    await ensureThreeLoaded();
    const { THREE, OrbitControls, GLTFLoader, OBJLoader, STLLoader, PLYLoader, FBXLoader, USDZLoader } = threeModules;

    cleanupRenderer();

    const width = container.clientWidth;
    const height = Math.min(width, 400);

    // Create canvas wrapper (for fullscreen isolation)
    const canvasWrapper = document.createElement('div');
    canvasWrapper.style.cssText = 'position:relative;width:100%;height:' + height + 'px;';
    const canvas = document.createElement('canvas');
    canvas.style.width = '100%';
    canvas.style.height = '100%';
    canvas.style.borderRadius = '8px';
    canvas.style.display = 'block';
    canvasWrapper.appendChild(canvas);
    container.appendChild(canvasWrapper);

    // Scene setup
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x2a2a3e);

    const camera = new THREE.PerspectiveCamera(50, width / height, 0.01, 1000);
    camera.position.set(0, 0, 3);

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.toneMapping = THREE.NeutralToneMapping;
    renderer.toneMappingExposure = 1.5;
    activeRenderer = renderer;

    // Lighting — boosted 3x for USD/USDZ converted models
    const lm = boostLighting ? 6.0 : 1.0;
    const ambient = new THREE.AmbientLight(0xffffff, 1.5 * lm);
    scene.add(ambient);
    const directional = new THREE.DirectionalLight(0xffffff, 1.2 * lm);
    directional.position.set(5, 5, 5);
    scene.add(directional);
    const backLight = new THREE.DirectionalLight(0xffffff, 0.6 * lm);
    backLight.position.set(-3, -2, -5);
    scene.add(backLight);
    const fillLight = new THREE.DirectionalLight(0xffffff, 0.4 * lm);
    fillLight.position.set(-5, 3, 0);
    scene.add(fillLight);

    // Grid helper
    const grid = new THREE.GridHelper(10, 20, 0x444466, 0x333355);
    scene.add(grid);

    // Controls
    const controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;
    controls.dampingFactor = 0.05;

    // Load model
    let loader;
    switch (ext) {
        case 'glb': case 'gltf':
            loader = new GLTFLoader();
            break;
        case 'obj':
            loader = new OBJLoader();
            break;
        case 'stl':
            loader = new STLLoader();
            break;
        case 'ply':
            loader = new PLYLoader();
            break;
        case 'fbx':
            loader = new FBXLoader();
            break;
        case 'usdz':
        case 'usd':
        case 'usda':
        case 'usdc':
            // USD files: try server-generated thumbnail/GLB first, show message if not available
            return renderUSDPreview(container, url, ext, cogId);
        default:
            loader = new GLTFLoader();
    }

    try {
        // Model bytes come from the same-origin authorised stream route (never a
        // presigned MinIO host — that XHR is cross-origin and blocked).
        const result = useStream
            ? await (async () => {
                const streamResp = await fetch(getStreamUrl(cogId));
                if (!streamResp.ok) throw new Error(`Stream failed: ${streamResp.status}`);
                return parseModelBytes(loader, ext, await streamResp.arrayBuffer());
            })()
            : await loadModelFromUrl(loader, url);

        let object;
        if (result.scene) {
            // GLTF/GLB
            object = result.scene;
        } else if (result.isBufferGeometry) {
            // STL/PLY return geometry
            result.computeVertexNormals();
            const mat = new THREE.MeshStandardMaterial({ color: 0x888888, side: THREE.DoubleSide });
            object = new THREE.Mesh(result, mat);
        } else {
            // OBJ/FBX return Object3D
            object = result;
        }

        // Center and scale to fit
        const box = new THREE.Box3().setFromObject(object);
        const center = box.getCenter(new THREE.Vector3());
        const size = box.getSize(new THREE.Vector3());
        const maxDim = Math.max(size.x, size.y, size.z);
        const scale = 2 / maxDim;

        object.position.sub(center);
        object.scale.multiplyScalar(scale);
        scene.add(object);

        camera.position.set(0, 1, 3);
        controls.target.set(0, 0, 0);
        controls.update();
    } catch (err) {
        console.error('Failed to load 3D model:', err);
        // For USD/USDZ: Three.js can't handle binary USDC — fall back to server thumbnail
        if (cogId && ['usd', 'usda', 'usdc', 'usdz'].includes(ext)) {
            container.innerHTML = `<div class="text-center text-gray-400 py-8 text-sm">
                <div class="animate-pulse">Requesting server-side preview...</div>
                <div class="text-xs mt-1">Binary USD files require server rendering</div>
            </div>`;
            try {
                const token = sessionStorage.getItem("accessToken");
                // Check for existing thumbnail first
                const thumbResp = await fetch(`${API_BASE_URL}/assets/${cogId}/thumbnail`, {
                    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
                });
                if (thumbResp.ok) {
                    const thumbData = await thumbResp.json();
                    if (thumbData?.url) {
                        container.innerHTML = '';
                        renderImagePreview(container, thumbData.url, ext + ' preview', cogId);
                        return;
                    }
                }
                // No thumbnail yet — show message
                container.innerHTML = `<div class="text-center text-gray-400 py-8 text-sm">
                    Server preview not available yet.<br>
                    <span class="text-xs">Upload triggers automatic thumbnail generation.</span>
                </div>`;
            } catch {
                container.innerHTML = `<div class="text-center text-gray-400 py-8 text-sm">Preview not available for binary USD files</div>`;
            }
            return;
        }
        container.innerHTML = `<div class="text-center text-red-500 py-8 text-sm">Failed to load 3D model</div>`;
        return;
    }

    // Fullscreen button (bottom-right corner)
    const fsBtn = document.createElement('button');
    fsBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>`;
    fsBtn.style.cssText = 'position:absolute;bottom:8px;right:8px;padding:6px;background:rgba(0,0,0,0.5);color:white;border:none;border-radius:6px;cursor:pointer;z-index:10;opacity:0.6;transition:opacity 0.2s;';
    fsBtn.onmouseenter = () => fsBtn.style.opacity = '1';
    fsBtn.onmouseleave = () => fsBtn.style.opacity = '0.6';
    fsBtn.title = 'Fullscreen';
    fsBtn.onclick = () => {
        canvasWrapper.style.background = '#1a1a2e';
        if (canvasWrapper.requestFullscreen) {
            canvasWrapper.requestFullscreen();
        } else if (canvasWrapper.webkitRequestFullscreen) {
            canvasWrapper.webkitRequestFullscreen();
        }
    };
    canvasWrapper.appendChild(fsBtn);

    // Handle fullscreen resize
    document.addEventListener('fullscreenchange', () => {
        if (document.fullscreenElement === canvasWrapper) {
            canvasWrapper.style.width = '100vw';
            canvasWrapper.style.height = '100vh';
            canvas.style.borderRadius = '0';
            const w = window.innerWidth;
            const h = window.innerHeight;
            camera.aspect = w / h;
            camera.updateProjectionMatrix();
            renderer.setSize(w, h);
        } else {
            canvasWrapper.style.width = '100%';
            canvasWrapper.style.height = height + 'px';
            canvas.style.borderRadius = '8px';
            const w = container.clientWidth;
            const h = Math.min(w, 400);
            camera.aspect = w / h;
            camera.updateProjectionMatrix();
            renderer.setSize(w, h);
        }
    });

    // Animation loop
    function animate() {
        activeAnimationId = requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }
    animate();

    // Resize handler (skip during fullscreen — fullscreenchange handles that)
    const resizeObserver = new ResizeObserver(() => {
        if (document.fullscreenElement) return;
        const w = container.clientWidth;
        const h = Math.min(w, 400);
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        renderer.setSize(w, h);
    });
    resizeObserver.observe(container);

    // "Set as Preview" button (guard against duplicate renders)
    if (cogId && !container.querySelector('#preview-btn-container')) {
        const btnContainer = document.createElement('div');
        btnContainer.id = 'preview-btn-container';
        btnContainer.style.cssText = 'display:flex; justify-content:center; margin-top:8px;';

        const btn = document.createElement('button');
        btn.textContent = 'Set as Preview';
        btn.style.cssText = 'padding:6px 16px; background:#6366f1; color:white; border:none; border-radius:6px; font-size:13px; font-weight:600; cursor:pointer;';
        btn.onmouseenter = () => btn.style.background = '#4f46e5';
        btn.onmouseleave = () => btn.style.background = '#6366f1';

        btn.onclick = async () => {
            btn.textContent = 'Saving...';
            btn.disabled = true;
            try {
                // Force a render so the canvas has content
                renderer.render(scene, camera);

                canvas.toBlob(async (blob) => {
                    if (!blob) { btn.textContent = 'Failed'; return; }

                    const formData = new FormData();
                    formData.append('file', blob, 'thumbnail.png');

                    const token = sessionStorage.getItem('accessToken');
                    const resp = await fetch(`${API_BASE_URL}/assets/${cogId}/thumbnail`, {
                        method: 'POST',
                        headers: { Authorization: `Bearer ${token}` },
                        body: formData,
                    });

                    if (resp.ok) {
                        btn.textContent = 'Saved!';
                        btn.style.background = '#10b981';
                        // Invalidate cache and update grid thumbnail immediately
                        thumbnailCache.delete(cogId);
                        thumbnailCache.delete(parseInt(cogId));
                        thumbnailCache.delete(String(cogId));
                        // Fetch new presigned thumbnail URL and update grid cell
                        try {
                            const thumbResp = await fetch(`${API_BASE_URL}/assets/${cogId}/thumbnail`, {
                                headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
                            });
                            if (thumbResp.ok) {
                                const thumbData = await thumbResp.json();
                                if (thumbData?.url) {
                                    thumbnailCache.set(cogId, thumbData.url);
                                    thumbnailCache.set(String(cogId), thumbData.url);
                                    // Find the grid cell and update it
                                    const gridCell = document.querySelector(`[data-id="${cogId}"] .thumbnail-slot`);
                                    if (gridCell) {
                                        applyThumbnail(gridCell, thumbData.url, 'image');
                                    }
                                }
                            }
                        } catch (e) { /* grid will update on next page load */ }
                    } else {
                        btn.textContent = 'Failed';
                        btn.style.background = '#ef4444';
                    }
                    setTimeout(() => {
                        btn.textContent = 'Set as Preview';
                        btn.style.background = '#6366f1';
                        btn.disabled = false;
                    }, 2000);
                }, 'image/png');
            } catch (e) {
                btn.textContent = 'Failed';
                btn.disabled = false;
            }
        };

        btnContainer.appendChild(btn);

        // For PLY files, add "View as Splat" button
        if (ext === 'ply') {
            const splatBtn = document.createElement('button');
            splatBtn.textContent = 'View as Splat';
            splatBtn.style.cssText = 'padding:6px 16px; background:#8b5cf6; color:white; border:none; border-radius:6px; font-size:13px; font-weight:600; cursor:pointer; margin-left:8px;';
            splatBtn.onmouseenter = () => splatBtn.style.background = '#7c3aed';
            splatBtn.onmouseleave = () => splatBtn.style.background = '#8b5cf6';
            splatBtn.onclick = () => {
                // Replace the 3D preview with SuperSplat iframe
                container.innerHTML = '';
                renderSplatPreview(container, url, cogId, `file.${ext}`);
            };
            btnContainer.appendChild(splatBtn);
        }

        container.appendChild(btnContainer);
    }
}

// -----------------------------------------------------------------------
// Image sequence detection + player
// -----------------------------------------------------------------------
//
// Numbered-sequence detection on a clicked image. If the parent
// folder has at least 2 sibling files sharing the same stem, the
// same separator (`_`, `.`, or none), and the same extension, with
// contiguous-or-near-contiguous numeric indices, we treat them as
// a sequence and render a scrubber + play/pause UI instead of a
// single still. Falls back gracefully to single-image preview when
// the file is alone or the pattern doesn't match.
//
// Pattern: <stem><sep><frame>.<ext>
//   sep ∈ {"_", ".", ""}; frame is the trailing run of digits.

const SEQUENCE_FRAME_RE = /^(.*?)([._]?)(\d+)\.([^.]+)$/;
let _activeSequencePlayer = null;  // for cleanup

function parseSequenceParts(filename) {
    if (!filename) return null;
    const m = filename.match(SEQUENCE_FRAME_RE);
    if (!m) return null;
    const [, stem, sep, frameStr, ext] = m;
    const padded = frameStr;
    const frame = parseInt(frameStr, 10);
    if (Number.isNaN(frame)) return null;
    // Don't treat short trailing numbers attached to a stem-less
    // start (e.g., "9.png") as a sequence; require a stem of ≥1
    // alphanumeric or a real separator before the digits.
    if (!stem && !sep) return null;
    return {
        stem,
        sep,
        ext: ext.toLowerCase(),
        frame,
        padding: padded.length,
    };
}

async function fetchSiblingsForSequence(item) {
    if (!item || item.parent_id == null) return [];
    const token = sessionStorage.getItem('accessToken');
    const params = new URLSearchParams({
        parent_id: String(item.parent_id),
        limit: '1000',
    });
    const resp = await fetch(`${API_BASE_URL}/assets?${params}`, {
        headers: { 'Authorization': `Bearer ${token}` },
    });
    if (!resp.ok) return [];
    const list = await resp.json();
    return Array.isArray(list) ? list : [];
}

/**
 * Returns the sequence frames sharing stem+sep+ext with `item`,
 * sorted by frame number, or null if the file isn't part of a
 * detectable sequence (no qualifying siblings).
 */
/**
 * #19 — Detect a frame-sequence pattern across the children of a
 * folder. Returns a summary {base, extension, frame_count,
 * first_frame_cog_id} on a confident match, or null otherwise.
 *
 * Pattern: every (or near every) child file's name parses to the
 * same stem + zero-padded numeric suffix + the same image extension.
 * Mixed folders (some sequence-shaped, some unrelated content)
 * SHOULD NOT trigger detection — keep the existing folder card.
 */
async function detectFolderFrameSequence(folderCogId) {
    if (!folderCogId) return null;
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem('accessToken');
    const resp = await fetch(`${apiUrl}/assets/?parent_id=${folderCogId}`, {
        headers: { 'Authorization': `Bearer ${token}` },
    });
    if (!resp.ok) return null;
    const items = await resp.json();
    if (!Array.isArray(items) || items.length < 2) return null;

    // Filter to file-type children only (folders inside folders
    // disqualify the sequence interpretation).
    const fileChildren = items.filter(
        (c) => c.cog_type === 'file' && typeof c.name === 'string',
    );
    if (fileChildren.length < 2) return null;
    if (fileChildren.length !== items.length) {
        // Mixed (some folders) — ambiguous. Don't detect.
        return null;
    }

    const SEQ_RE = /^(.+?)[-._]?(\d{2,})\.(png|jpg|jpeg|tiff?|exr|bmp|webp)$/i;
    const matches = [];
    let firstStem = null;
    let firstExt = null;
    for (const f of fileChildren) {
        const m = SEQ_RE.exec(f.name);
        if (!m) return null;  // non-sequence file → not a sequence folder
        const [, stem, , ext] = m;
        const lowExt = ext.toLowerCase();
        if (firstStem === null) {
            firstStem = stem;
            firstExt = lowExt;
        } else if (stem !== firstStem || lowExt !== firstExt) {
            return null;  // inconsistent stem/ext → not the same sequence
        }
        matches.push({ id: f.id, frame: parseInt(m[2], 10) });
    }
    if (matches.length < 2) return null;
    matches.sort((a, b) => a.frame - b.frame);
    return {
        base: firstStem,
        extension: firstExt,
        frame_count: matches.length,
        first_frame_cog_id: matches[0].id,
    };
}


async function detectImageSequence(item) {
    const parts = parseSequenceParts(item.name);
    if (!parts) return null;
    if (!BROWSER_IMAGE_EXTS.has(parts.ext)) return null;
    const siblings = await fetchSiblingsForSequence(item);
    const matches = [];
    for (const sib of siblings) {
        const sp = parseSequenceParts(sib.name);
        if (!sp) continue;
        if (sp.stem !== parts.stem) continue;
        if (sp.sep !== parts.sep) continue;
        // Require matching extension. Even when stems collide across
        // formats (e.g., name_0001.png + name_0001.jpg), we only
        // play the ext the user opened.
        if (sp.ext !== parts.ext) continue;
        matches.push({ ...sib, _frame: sp.frame, _padding: sp.padding });
    }
    if (matches.length < 2) return null;
    matches.sort((a, b) => a._frame - b._frame);
    return {
        stem: parts.stem,
        sep: parts.sep,
        ext: parts.ext,
        frames: matches,
        firstFrame: matches[0]._frame,
        lastFrame: matches[matches.length - 1]._frame,
        padding: parts.padding,
    };
}

/**
 * Render a scrubbable image-sequence player into `container`.
 * Lazy-loads each frame's presigned URL on demand and caches.
 */
async function renderImageSequencePreview(container, sequence, openedCogId) {
    container.innerHTML = '';
    const wrap = document.createElement('div');
    wrap.dataset.role = 'sequence-player';
    container.appendChild(wrap);

    const header = document.createElement('div');
    header.style.cssText =
        'font-size:11px;color:#6b7280;display:flex;justify-content:space-between;align-items:center;padding:4px 0;';
    header.innerHTML = `
        <span>Sequence: <strong>${sequence.stem || '(unnamed)'}</strong> &middot; ${sequence.frames.length} frames (${sequence.firstFrame}–${sequence.lastFrame}).${sequence.ext}</span>
        <span data-role="frame-counter" style="font-variant-numeric:tabular-nums;"></span>
    `;
    wrap.appendChild(header);

    const img = document.createElement('img');
    img.alt = 'Sequence frame';
    img.className = 'w-full rounded-lg object-contain max-h-96 bg-black';
    wrap.appendChild(img);

    // Build a frame-index → bool map of which sibling indices are
    // present, so the scrubber can mark gaps. Frame `i` in the
    // visible scrubber corresponds to frames[i].
    const gapMarkers = document.createElement('div');
    gapMarkers.style.cssText = 'position:relative;height:6px;margin-top:6px;';
    // The set of frame numbers that exist; used to compute gaps in
    // the integer range [first..last].
    const presentFrameNumbers = new Set(sequence.frames.map(f => f._frame));
    const totalRange = sequence.lastFrame - sequence.firstFrame + 1;
    const gaps = [];
    for (let n = sequence.firstFrame; n <= sequence.lastFrame; n++) {
        if (!presentFrameNumbers.has(n)) gaps.push(n);
    }
    if (gaps.length > 0) {
        // Render each gap as a thin grey dash on the scrubber track.
        const track = document.createElement('div');
        track.style.cssText =
            'position:absolute;left:0;right:0;top:2px;height:2px;background:#e5e7eb;border-radius:1px;';
        gapMarkers.appendChild(track);
        for (const n of gaps) {
            const pct = ((n - sequence.firstFrame) / Math.max(1, totalRange - 1)) * 100;
            const mark = document.createElement('div');
            mark.title = `Missing frame ${n}`;
            mark.style.cssText =
                `position:absolute;top:0;left:${pct}%;width:1.5px;height:6px;background:#f59e0b;`;
            gapMarkers.appendChild(mark);
        }
        wrap.appendChild(gapMarkers);
    }

    const scrubber = document.createElement('input');
    scrubber.type = 'range';
    scrubber.min = '0';
    scrubber.max = String(sequence.frames.length - 1);
    scrubber.step = '1';
    scrubber.value = '0';
    scrubber.style.cssText = 'width:100%;margin-top:6px;';
    wrap.appendChild(scrubber);

    const controls = document.createElement('div');
    controls.style.cssText =
        'display:flex;align-items:center;gap:8px;font-size:12px;color:#374151;margin-top:8px;';
    controls.innerHTML = `
        <button data-act="prev" aria-label="Previous frame" class="px-2 py-1 rounded border border-gray-300 hover:bg-gray-50">‹</button>
        <button data-act="play"  class="px-2 py-1 rounded border border-gray-300 bg-primary text-white hover:bg-primary/90">Play</button>
        <button data-act="next" aria-label="Next frame" class="px-2 py-1 rounded border border-gray-300 hover:bg-gray-50">›</button>
        <label class="flex items-center gap-1 ml-auto">
            <span>fps</span>
            <input data-act="fps" type="number" min="1" max="120" value="24" style="width:56px;font-variant-numeric:tabular-nums;" class="border border-gray-300 rounded px-1.5 py-0.5 text-xs"/>
        </label>
    `;
    wrap.appendChild(controls);

    const counter = header.querySelector('[data-role="frame-counter"]');

    // Frame URL cache so scrubbing doesn't re-fetch the same frame.
    const urlCache = new Map();
    let activeIndex = 0;
    let playing = false;
    let timer = null;

    const updateCounter = () => {
        const f = sequence.frames[activeIndex];
        counter.textContent = `frame ${f._frame} (${activeIndex + 1}/${sequence.frames.length})`;
    };

    const showFrame = async (index) => {
        if (index < 0 || index >= sequence.frames.length) return;
        activeIndex = index;
        scrubber.value = String(index);
        updateCounter();
        const frame = sequence.frames[index];
        let u = urlCache.get(frame.id);
        if (!u) {
            try {
                u = await getPresignedUrl(frame.id);
                urlCache.set(frame.id, u);
            } catch {
                return;
            }
        }
        // Bail if a different file got opened in the meantime.
        if (currentPreviewCogId !== openedCogId) return;
        img.src = u;
    };

    const stop = () => {
        playing = false;
        if (timer) { clearInterval(timer); timer = null; }
        const playBtn = controls.querySelector('[data-act="play"]');
        if (playBtn) playBtn.textContent = 'Play';
    };

    const start = () => {
        if (playing) return;
        playing = true;
        const playBtn = controls.querySelector('[data-act="play"]');
        if (playBtn) playBtn.textContent = 'Pause';
        const fpsInput = controls.querySelector('[data-act="fps"]');
        const fps = Math.max(1, Math.min(120, parseInt(fpsInput.value, 10) || 24));
        timer = setInterval(() => {
            const next = (activeIndex + 1) % sequence.frames.length;
            showFrame(next);
        }, Math.round(1000 / fps));
    };

    scrubber.addEventListener('input', () => {
        stop();
        showFrame(parseInt(scrubber.value, 10) || 0);
    });
    controls.querySelector('[data-act="prev"]').addEventListener('click', () => {
        stop();
        showFrame(Math.max(0, activeIndex - 1));
    });
    controls.querySelector('[data-act="next"]').addEventListener('click', () => {
        stop();
        showFrame(Math.min(sequence.frames.length - 1, activeIndex + 1));
    });
    controls.querySelector('[data-act="play"]').addEventListener('click', () => {
        if (playing) stop();
        else start();
    });
    controls.querySelector('[data-act="fps"]').addEventListener('change', () => {
        if (playing) { stop(); start(); }
    });

    _activeSequencePlayer = { stop };

    // Start at the frame the user actually opened (so clicking
    // frame_0042 doesn't drop them at frame_0001).
    const openedIndex = sequence.frames.findIndex(f => f.id === openedCogId);
    await showFrame(openedIndex >= 0 ? openedIndex : 0);
}

function cleanupSequencePlayer() {
    if (_activeSequencePlayer && _activeSequencePlayer.stop) {
        _activeSequencePlayer.stop();
    }
    _activeSequencePlayer = null;
}

// -----------------------------------------------------------------------
// Image preview
// -----------------------------------------------------------------------
function renderImagePreview(container, url, filename, cogId, retried) {
    const img = document.createElement('img');
    img.src = url;
    img.alt = filename || 'Preview';
    img.className = 'w-full rounded-lg object-contain max-h-96';
    // Capture natural dimensions for the Details tab. naturalWidth /
    // naturalHeight are populated by the time `load` fires, regardless
    // of the CSS-scaled rendered size.
    img.addEventListener('load', () => {
        _mediaInfo.width = img.naturalWidth || null;
        _mediaInfo.height = img.naturalHeight || null;
        populateDetailsTab();
    }, { once: true });
    img.onerror = async () => {
        // Auto-retry with a fresh presigned URL (the old one may have expired)
        if (!retried && cogId) {
            console.warn('Image preview failed, fetching fresh URL for:', filename);
            try {
                const freshUrl = await getPresignedUrl(cogId);
                container.innerHTML = '';
                renderImagePreview(container, freshUrl, filename, cogId, true);
                return;
            } catch (e) {
                console.error('Fresh URL fetch also failed:', e);
            }
        }
        // Final failure — try to trigger preview regeneration
        if (cogId) {
            container.innerHTML = `<div class="text-center text-gray-400 py-8 text-sm">
                <div class="animate-pulse">Regenerating preview...</div>
            </div>`;
            try {
                const token = sessionStorage.getItem("accessToken");
                // Request a fresh thumbnail generation
                await fetch(`${API_BASE_URL}/assets/${cogId}/thumbnail`, {
                    method: 'POST',
                    headers: { Authorization: `Bearer ${token}` },
                });
                // Wait a few seconds then retry with the newly generated thumbnail
                setTimeout(async () => {
                    try {
                        const thumbResp = await fetch(`${API_BASE_URL}/assets/${cogId}/thumbnail`, {
                            headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
                        });
                        if (thumbResp.ok) {
                            const thumbData = await thumbResp.json();
                            if (thumbData?.url) {
                                container.innerHTML = '';
                                renderImagePreview(container, thumbData.url, filename, null, true);
                                return;
                            }
                        }
                        container.innerHTML = `<div class="text-center text-gray-400 py-8 text-sm">Preview unavailable</div>`;
                    } catch {
                        container.innerHTML = `<div class="text-center text-gray-400 py-8 text-sm">Preview unavailable</div>`;
                    }
                }, 5000);
                return;
            } catch {
                // Regeneration request failed — show error
            }
        }
        container.innerHTML = `<div class="text-center text-gray-400 py-8 text-sm">Preview unavailable</div>`;
    };
    container.appendChild(img);
}

// -----------------------------------------------------------------------
// Video preview
// -----------------------------------------------------------------------
function renderVideoPreview(container, url) {
    const video = document.createElement('video');
    video.src = url;
    video.className = 'w-full rounded-lg max-h-96';
    video.controls = true;
    video.muted = true;
    video.playsInline = true;
    video.preload = 'metadata';
    // `loadedmetadata` fires as soon as the browser has decoded enough
    // of the container to know dimensions + duration — well before
    // the video is fully buffered. Capture both for the Details tab.
    video.addEventListener('loadedmetadata', () => {
        _mediaInfo.width = video.videoWidth || null;
        _mediaInfo.height = video.videoHeight || null;
        _mediaInfo.duration_s = Number.isFinite(video.duration) ? video.duration : null;
        populateDetailsTab();
    }, { once: true });
    video.onerror = () => {
        container.innerHTML = `<div class="text-center text-gray-400 py-8 text-sm">Cannot play this video format in browser</div>`;
    };
    container.appendChild(video);
}

// -----------------------------------------------------------------------
// Text/code file preview
// -----------------------------------------------------------------------
async function renderTextPreview(container, url, filename, ext, cogId) {
    try {
        // Fetch text via API to avoid CORS issues with presigned URLs
        let resp;
        if (cogId) {
            const token = sessionStorage.getItem("accessToken");
            resp = await fetch(`${API_BASE_URL}/assets/${cogId}/text-content`, {
                headers: { Authorization: `Bearer ${token}` },
            });
        } else {
            resp = await fetch(url);
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

        let text = await resp.text();

        // Truncate very large files
        const MAX_CHARS = 100000;
        let truncated = false;
        if (text.length > MAX_CHARS) {
            text = text.substring(0, MAX_CHARS);
            truncated = true;
        }

        // Detect language for styling
        const lang = getLanguageLabel(ext);

        // For JSON, try to pretty-print
        if (ext === 'json') {
            try {
                text = JSON.stringify(JSON.parse(text), null, 2);
            } catch (e) { /* not valid JSON, show raw */ }
        }

        // For CSV, show as a table
        if (ext === 'csv') {
            container.innerHTML = renderCSVTable(text, truncated);
            return;
        }

        // Escape HTML
        const escaped = text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');

        // Add line numbers
        const lines = escaped.split('\n');
        const numbered = lines.map((line, i) =>
            `<span style="color:#6b7280;user-select:none;display:inline-block;width:3em;text-align:right;margin-right:1em;">${i + 1}</span>${line}`
        ).join('\n');

        container.innerHTML = `
            <div style="position:relative;">
                <div style="position:absolute;top:8px;right:8px;display:flex;gap:6px;z-index:1;">
                    <span style="background:#374151;color:#9ca3af;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;">${lang}</span>
                    <button onclick="window.modCopyText(decodeURIComponent('${encodeURIComponent(text)}'), this)" style="background:#374151;color:#9ca3af;padding:2px 8px;border-radius:4px;font-size:11px;cursor:pointer;border:none;" onmouseenter="this.style.color='#fff'" onmouseleave="this.style.color='#9ca3af'">Copy</button>
                </div>
                <pre style="background:#111827;color:#e5e7eb;padding:16px;border-radius:8px;overflow:auto;max-height:500px;font-size:12px;line-height:1.5;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;margin:0;tab-size:4;">${numbered}</pre>
                ${truncated ? '<p style="text-align:center;color:#6b7280;font-size:11px;margin-top:4px;">File truncated — showing first 100,000 characters</p>' : ''}
            </div>
        `;
    } catch (err) {
        container.innerHTML = `<div class="text-center text-red-500 py-8 text-sm">Failed to load text file: ${_escapeHtml(err.message)}</div>`;
    }
}

function renderCSVTable(text, truncated) {
    const rows = text.split('\n').filter(r => r.trim());
    if (rows.length === 0) return '<div class="text-center text-gray-400 py-8">Empty file</div>';

    const maxRows = 200;
    const displayRows = rows.slice(0, maxRows);
    const isCSVTruncated = rows.length > maxRows || truncated;

    // Parse CSV (simple — doesn't handle quoted commas)
    const parseRow = (row) => row.split(',').map(cell => cell.trim().replace(/^"|"$/g, ''));

    const header = parseRow(displayRows[0]);
    const body = displayRows.slice(1).map(parseRow);

    let html = `<div style="overflow:auto;max-height:500px;border-radius:8px;border:1px solid #e5e7eb;">`;
    html += `<table style="width:100%;border-collapse:collapse;font-size:12px;">`;
    html += `<thead><tr style="background:#f3f4f6;position:sticky;top:0;">`;
    header.forEach(h => {
        html += `<th style="padding:6px 10px;text-align:left;font-weight:600;color:#374151;border-bottom:2px solid #d1d5db;white-space:nowrap;">${h.replace(/</g, '&lt;')}</th>`;
    });
    html += `</tr></thead><tbody>`;
    body.forEach((row, i) => {
        const bg = i % 2 === 0 ? '#fff' : '#f9fafb';
        html += `<tr style="background:${bg};">`;
        row.forEach(cell => {
            html += `<td style="padding:4px 10px;color:#4b5563;border-bottom:1px solid #f3f4f6;white-space:nowrap;">${cell.replace(/</g, '&lt;')}</td>`;
        });
        html += `</tr>`;
    });
    html += `</tbody></table></div>`;
    if (isCSVTruncated) {
        html += `<p style="text-align:center;color:#6b7280;font-size:11px;margin-top:4px;">Showing first ${Math.min(maxRows, body.length)} of ${rows.length - 1} rows</p>`;
    }
    return html;
}

function getLanguageLabel(ext) {
    const map = {
        'py': 'Python', 'js': 'JavaScript', 'ts': 'TypeScript', 'jsx': 'JSX', 'tsx': 'TSX',
        'json': 'JSON', 'yaml': 'YAML', 'yml': 'YAML', 'xml': 'XML', 'html': 'HTML',
        'css': 'CSS', 'scss': 'SCSS', 'less': 'LESS', 'csv': 'CSV', 'sql': 'SQL',
        'sh': 'Shell', 'bash': 'Bash', 'go': 'Go', 'rs': 'Rust', 'rb': 'Ruby',
        'java': 'Java', 'c': 'C', 'cpp': 'C++', 'h': 'C Header', 'hpp': 'C++ Header',
        'cs': 'C#', 'php': 'PHP', 'pl': 'Perl', 'lua': 'Lua', 'r': 'R',
        'toml': 'TOML', 'ini': 'INI', 'cfg': 'Config', 'conf': 'Config',
        'md': 'Markdown', 'markdown': 'Markdown', 'txt': 'Text', 'log': 'Log',
        'dockerfile': 'Dockerfile', 'makefile': 'Makefile', 'graphql': 'GraphQL',
        'proto': 'Protobuf', 'env': 'Env', 'gitignore': 'Git Ignore',
    };
    return map[ext] || ext.toUpperCase();
}

// -----------------------------------------------------------------------
// Point cloud preview (Three.js Points renderer)
// -----------------------------------------------------------------------
//
// Supports PCD (PCDLoader), PLY, and XYZ (PLYLoader). LAS/LAZ require
// a separate WASM loader that isn't bundled — they render a clear
// "not yet supported" panel instead of silently failing through the
// PLY path.
const POINTCLOUD_RENDER_BUDGET = 1_000_000;  // points; decimate above

// In-session cache of decoded point-cloud geometry, keyed by cog id.
// Cache hit → re-renders instantly instead of re-fetching the 95MB PLY
// and re-parsing. Cleared on page reload (don't want this to grow
// unbounded across navigations).
const pointCloudGeometryCache = new Map();

// Stride-based subsample of a point-cloud BufferGeometry down to roughly
// `budget` points. Stride is uniform (every Nth point) so the spatial
// distribution survives — random shuffles would scatter the surface.
function decimatePointCloudGeometry(THREE, geometry, budget) {
    const posAttr = geometry.attributes.position;
    if (!posAttr || posAttr.count <= budget) return geometry;
    const stride = Math.ceil(posAttr.count / budget);
    const colorAttr = geometry.attributes.color;
    const newCount = Math.ceil(posAttr.count / stride);
    const positions = new Float32Array(newCount * 3);
    const colors = colorAttr ? new Float32Array(newCount * (colorAttr.itemSize || 3)) : null;
    let w = 0;
    for (let r = 0; r < posAttr.count; r += stride) {
        positions[w * 3 + 0] = posAttr.getX(r);
        positions[w * 3 + 1] = posAttr.getY(r);
        positions[w * 3 + 2] = posAttr.getZ(r);
        if (colors && colorAttr) {
            const cs = colorAttr.itemSize || 3;
            for (let c = 0; c < cs; c++) {
                // BufferAttribute.getComponent isn't always exposed
                // across THREE versions; fall back to the array view.
                colors[w * cs + c] = colorAttr.array[r * cs + c];
            }
        }
        w++;
    }
    const out = new THREE.BufferGeometry();
    out.setAttribute('position', new THREE.BufferAttribute(positions.subarray(0, w * 3), 3));
    if (colors && colorAttr) {
        out.setAttribute('color', new THREE.BufferAttribute(
            colors.subarray(0, w * (colorAttr.itemSize || 3)),
            colorAttr.itemSize || 3,
        ));
    }
    return out;
}

// GSEC FX.19 R-042 (EGRESS_INVENTORY row 3): loaders.gl is served from the
// local vendor tree — see CORE/Dashboard/vendor/loaders.gl/README.md for
// versions and rebuild commands (never a public CDN).
const LOADERS_GL_CORE_PATH = '/vendor/loaders.gl/core.esm.js';
const LOADERS_GL_LAS_PATH = '/vendor/loaders.gl/las.esm.js';

async function renderPointCloudPreview(container, url, ext, cogId) {
    // #7 (round 11) — split routing:
    //   • LAS / LAZ → loaders.gl in-browser (no conversion wait — the
    //     LAZ WASM bundle is ~2.5 MB, downloaded once, then cached).
    //   • E57 → server-side converter (no good open-source browser lib
    //     for E57; falls back to /cogs/{id}/converted-pointcloud).
    //   • PCD / PLY / XYZ → native Three.js loaders, unchanged.
    // The end state is the same in all paths: a Three.js BufferGeometry
    // fed into the existing point-cloud scene below.
    let lasGeometry = null;
    if (ext === 'las' || ext === 'laz') {
        await ensureThreeLoaded();
        const { THREE: THREE_PRE } = threeModules;
        try {
            const [{ LASLoader }, { load }] = await Promise.all([
                import(LOADERS_GL_LAS_PATH),
                import(LOADERS_GL_CORE_PATH),
            ]);
            // worker: false — parse on the main thread with the inlined
            // asm.js laz decoder; avoids loaders.gl's default unpkg
            // worker-script fetch (EGRESS_INVENTORY row 3).
            const data = await load(url, LASLoader, { worker: false });
            const positions = data?.attributes?.POSITION?.value;
            if (!positions || positions.length === 0) {
                throw new Error('loaders.gl returned no POSITION attribute');
            }
            lasGeometry = new THREE_PRE.BufferGeometry();
            lasGeometry.setAttribute(
                'position',
                new THREE_PRE.BufferAttribute(positions, 3),
            );
            const colors = data?.attributes?.COLOR_0?.value;
            if (colors && colors.length === positions.length) {
                // loaders.gl LAS color is uint16 (0..65535) per channel.
                // Three.js wants float (0..1), so normalize the same way
                // we'd normalize a Three.js BufferAttribute with
                // `normalized=true` (last arg).
                lasGeometry.setAttribute(
                    'color',
                    new THREE_PRE.BufferAttribute(colors, 3, true),
                );
            }
        } catch (err) {
            console.warn('loaders.gl LAS/LAZ load failed:', err);
            // Failed import (vendor bundle not populated) or parse error —
            // degrade to the existing unsupported-type fallback panel
            // ("preview unavailable" / download original).
            _renderCADFallbackPanel(
                container,
                { id: cogId, name: '' },
                ext,
                { reason: err.message || String(err) },
            );
            return;
        }
    } else if (ext === 'e57') {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        let resp;
        try {
            resp = await fetch(`${apiUrl}/assets/${cogId}/converted-pointcloud`, {
                headers: { 'Authorization': `Bearer ${token}` },
            });
        } catch {
            container.innerHTML = `
                <div style="padding:24px;text-align:center;color:#dc2626;font-size:13px;">
                    Network error fetching converted point cloud.
                </div>`;
            return;
        }
        if (resp.status === 202) {
            _renderPointCloudPendingPanel(container, cogId, ext);
            return;
        }
        if (resp.status === 422) {
            let body = {};
            try { body = await resp.json(); } catch {}
            container.innerHTML = `
                <div style="padding:24px;text-align:center;color:#9ca3af;font-size:13px;">
                    <p style="margin-bottom:8px;font-weight:500;color:#dc2626;">Point cloud conversion failed</p>
                    <p>${escapeHtmlAttr(body.error_message || 'Unknown error')}</p>
                </div>`;
            return;
        }
        if (!resp.ok) {
            container.innerHTML = `
                <div style="padding:24px;text-align:center;color:#9ca3af;font-size:13px;">
                    <p style="margin-bottom:8px;font-weight:500;color:#374151;">No conversion available — re-upload to trigger.</p>
                </div>`;
            return;
        }
        const body = await resp.json();
        url = body.url;
        ext = 'ply';
    }

    await ensureThreeLoaded();
    const { THREE, OrbitControls, PLYLoader } = threeModules;

    cleanupRenderer();

    const width = container.clientWidth;
    const height = Math.min(width, 400);

    // Spinner panel shown during the geometry download + parse step.
    // Conversion happens once on the server; this is just the browser
    // pulling the cached PLY and decoding it. Copy says "Loading" so
    // the user doesn't think it's reconverting on every click.
    container.innerHTML = `
        <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:300px; padding:24px; color:#444;">
            <div style="width:42px; height:42px; border:4px solid #e2e8f0; border-top-color:#6366f1; border-radius:50%; animation: pcp-spin 0.9s linear infinite;"></div>
            <div style="margin-top:16px; font-size:14px; font-weight:600; color:#1e293b;">Loading point cloud…</div>
            <div style="margin-top:6px; font-size:12px; color:#64748b; max-width:380px; text-align:center;">
                Decoding the cached point cloud. Larger files take 10–30 seconds.
            </div>
            <style>@keyframes pcp-spin { to { transform: rotate(360deg); } }</style>
        </div>
    `;

    try {
        let geometry;
        // In-session cache hit → skip fetch + parse entirely.
        // Cache stores the *original* (non-decimated, non-translated)
        // geometry so we can re-derive header stats and keep the
        // viewer behavior identical to a fresh load.
        const cached = cogId != null ? pointCloudGeometryCache.get(cogId) : null;
        if (cached) {
            geometry = cached.clone();
        } else if (lasGeometry) {
            // LAS / LAZ already loaded via loaders.gl above — geometry
            // is in-memory, no fetch needed.
            geometry = lasGeometry;
        } else if (ext === 'pcd') {
            const { PCDLoader } = await import('three/addons/loaders/PCDLoader.js');
            const loader = new PCDLoader();
            const points = await new Promise((resolve, reject) => {
                loader.load(url, resolve, undefined, reject);
            });
            geometry = points.geometry;
        } else {
            // PLY, XYZ — use PLYLoader
            const loader = new PLYLoader();
            geometry = await new Promise((resolve, reject) => {
                loader.load(url, resolve, undefined, reject);
            });
        }
        // Stash in cache (a clone so subsequent in-place ops don't
        // mutate the cached version). Skipped for LAS in-memory geometry
        // since it's already cached by loaders.gl's own internal cache.
        if (!cached && cogId != null && !lasGeometry) {
            try {
                pointCloudGeometryCache.set(cogId, geometry.clone());
            } catch { /* clone unsupported on this geometry shape */ }
        }

        // Capture the original count BEFORE any decimation so the
        // header can honestly show "showing X of Y" if we trim.
        const originalCount = geometry.attributes.position
            ? geometry.attributes.position.count
            : 0;
        let renderedCount = originalCount;
        let decimated = false;
        if (originalCount > POINTCLOUD_RENDER_BUDGET) {
            geometry = decimatePointCloudGeometry(THREE, geometry, POINTCLOUD_RENDER_BUDGET);
            renderedCount = geometry.attributes.position.count;
            decimated = true;
        }

        geometry.computeBoundingBox();
        const box = geometry.boundingBox;
        const center = box.getCenter(new THREE.Vector3());
        const size = box.getSize(new THREE.Vector3());
        const maxDim = Math.max(size.x, size.y, size.z);

        const fmt = (n) => Math.abs(n) >= 1000
            ? n.toExponential(2)
            : n.toFixed(3);
        const countStr = decimated
            ? `${renderedCount.toLocaleString()} of ${originalCount.toLocaleString()} points <span style="color:#f59e0b;">(decimated for preview)</span>`
            : `${renderedCount.toLocaleString()} points`;

        geometry.translate(-center.x, -center.y, -center.z);
        if (maxDim > 0) {
            const scale = 2 / maxDim;
            geometry.scale(scale, scale, scale);
        }

        // Geometry's actually loaded — swap the spinner panel for the
        // real viewer (header + canvas).
        container.innerHTML = '';
        const header = document.createElement('div');
        header.dataset.role = 'pointcloud-header';
        header.style.cssText = 'font-size:11px;color:#6b7280;display:flex;flex-wrap:wrap;gap:12px;padding:6px 4px;';
        header.innerHTML = `
            <span>${countStr}</span>
            <span>bbox: ${fmt(size.x)} &times; ${fmt(size.y)} &times; ${fmt(size.z)}</span>
        `;
        container.appendChild(header);

        const canvas = document.createElement('canvas');
        canvas.style.width = '100%';
        canvas.style.height = `${height}px`;
        canvas.style.borderRadius = '8px';
        container.appendChild(canvas);

        const scene = new THREE.Scene();
        scene.background = new THREE.Color(0x111827);
        const camera = new THREE.PerspectiveCamera(50, width / height, 0.01, 1000);
        camera.position.set(0, 0.5, 3);
        const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
        renderer.setSize(width, height);
        renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        activeRenderer = renderer;
        scene.add(new THREE.AmbientLight(0xffffff, 0.8));
        const controls = new OrbitControls(camera, canvas);
        controls.enableDamping = true;
        controls.target.set(0, 0, 0);
        controls.update();

        const hasColors = geometry.hasAttribute('color');
        const material = new THREE.PointsMaterial({
            size: 0.01,
            vertexColors: hasColors,
            color: hasColors ? 0xffffff : 0x88aaff,
            sizeAttenuation: true,
        });
        scene.add(new THREE.Points(geometry, material));

        function animate() {
            activeAnimationId = requestAnimationFrame(animate);
            controls.update();
            renderer.render(scene, camera);
        }
        animate();

        const resizeObserver = new ResizeObserver(() => {
            const w = container.clientWidth;
            const h = Math.min(w, 400);
            camera.aspect = w / h;
            camera.updateProjectionMatrix();
            renderer.setSize(w, h);
        });
        resizeObserver.observe(container);

        // Set as Preview button (inside the try so it has access to
        // the renderer/scene/canvas locals).
        if (cogId && !container.querySelector('#preview-btn-container')) {
            const btnContainer = document.createElement('div');
            btnContainer.id = 'preview-btn-container';
            btnContainer.style.cssText = 'display:flex;justify-content:center;margin-top:8px;';

            const btn = document.createElement('button');
            btn.textContent = 'Set as Preview';
            btn.style.cssText = 'padding:6px 16px;background:#6366f1;color:white;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;';
            btn.onclick = async () => {
                btn.textContent = 'Saving...';
                btn.disabled = true;
                renderer.render(scene, camera);
                canvas.toBlob(async (blob) => {
                    if (!blob) { btn.textContent = 'Failed'; return; }
                    const formData = new FormData();
                    formData.append('file', blob, 'thumbnail.png');
                    const token = sessionStorage.getItem('accessToken');
                    const resp = await fetch(`${API_BASE_URL}/assets/${cogId}/thumbnail`, {
                        method: 'POST',
                        headers: { Authorization: `Bearer ${token}` },
                        body: formData,
                    });
                    btn.textContent = resp.ok ? 'Saved!' : 'Failed';
                    btn.style.background = resp.ok ? '#10b981' : '#ef4444';
                    if (resp.ok) {
                        // Refresh the grid cell so the new thumbnail
                        // appears immediately. (1) invalidate the
                        // thumbnail cache, (2) re-fetch /thumbnail and
                        // swap the matching grid cell's image. Without
                        // this the user has to reload the page to see
                        // the saved thumbnail.
                        thumbnailCache.delete(cogId);
                        thumbnailCache.delete(String(cogId));
                        try {
                            const thumbResp = await fetch(`${API_BASE_URL}/assets/${cogId}/thumbnail`, {
                                headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
                            });
                            if (thumbResp.ok) {
                                const data = await thumbResp.json();
                                if (data?.url) {
                                    // Match the grid cell by data-id. Both
                                    // Dashboard and TrayApp render rows
                                    // with this attribute on the row OR on
                                    // a `.thumbnail-slot` child.
                                    const slots = document.querySelectorAll(
                                        `[data-id="${cogId}"] .thumbnail-slot, [data-id="${cogId}"][data-thumbnail-slot]`
                                    );
                                    slots.forEach(slot => applyThumbnail(slot, data.url, 'image'));
                                    // Cache-bust the URL too in case the
                                    // browser holds the prior PNG bytes.
                                    thumbnailCache.set(cogId, data.url);
                                }
                            }
                        } catch (e) { /* non-fatal — page reload would also work */ }
                    }
                    setTimeout(() => { btn.textContent = 'Set as Preview'; btn.style.background = '#6366f1'; btn.disabled = false; }, 2000);
                }, 'image/png');
            };
            btnContainer.appendChild(btn);
            container.appendChild(btnContainer);
        }
    } catch (err) {
        console.error('Point cloud load error:', err);
        container.innerHTML = `<div style="text-align:center;color:#ef4444;padding:24px;font-size:13px;">Failed to load point cloud: ${_escapeHtml(err.message)}</div>`;
        return;
    }
}

// -----------------------------------------------------------------------
// PLY chooser — mesh or Gaussian splat?
// -----------------------------------------------------------------------
function renderPLYChooser(container, url, ext, cogId) {
    container.innerHTML = `
        <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; height:300px; gap:16px;">
            <div style="font-size:48px;">🧊</div>
            <p style="color:#6b7280; font-size:14px; font-weight:500;">This PLY file can be viewed multiple ways.</p>
            <p style="color:#9ca3af; font-size:12px;">How would you like to view it?</p>
            <div style="display:flex; gap:12px; flex-wrap:wrap; justify-content:center;">
                <button id="ply-view-3d" style="padding:10px 24px; background:#6366f1; color:white; border:none; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer;">
                    🧊 3D Mesh
                </button>
                <button id="ply-view-pointcloud" style="padding:10px 24px; background:#3b82f6; color:white; border:none; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer;">
                    ✦ Point Cloud
                </button>
                <button id="ply-view-splat" style="padding:10px 24px; background:#8b5cf6; color:white; border:none; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer;">
                    ✨ Gaussian Splat
                </button>
            </div>
        </div>
    `;

    document.getElementById('ply-view-3d').onclick = async () => {
        container.innerHTML = '';
        await render3DPreview(container, url, ext, cogId);
    };

    document.getElementById('ply-view-pointcloud').onclick = async () => {
        container.innerHTML = '';
        await renderPointCloudPreview(container, url, ext, cogId);
    };

    document.getElementById('ply-view-splat').onclick = () => {
        container.innerHTML = '';
        renderSplatPreview(container, url, cogId, `file.${ext}`);
    };
}

// -----------------------------------------------------------------------
// Gaussian Splat preview (via SuperSplat hosted viewer)
// -----------------------------------------------------------------------
async function renderSplatPreview(container, url, cogId, filename) {
    // Native gsplat.js renderer — no iframe, no external site
    // Use presigned URL (no auth needed) instead of stream endpoint with token
    const token = sessionStorage.getItem("accessToken");
    let fileUrl = url;
    if (cogId && !url) {
        try {
            const resp = await fetch(`${API_BASE_URL}/assets/${cogId}/preview-url`, {
                headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
            });
            if (resp.ok) {
                const data = await resp.json();
                if (data?.url) fileUrl = data.url;
            }
        } catch (e) { /* fall back to original url */ }
    }

    const width = container.clientWidth || 600;
    const height = Math.min(width, 500);

    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    canvas.style.cssText = `width:100%;height:${height}px;border-radius:8px;background:#111;display:block;`;
    container.appendChild(canvas);

    // Loading indicator
    const loadingDiv = document.createElement('div');
    loadingDiv.style.cssText = 'text-align:center;color:#9ca3af;font-size:12px;margin-top:8px;';
    loadingDiv.textContent = 'Loading Gaussian splat...';
    container.appendChild(loadingDiv);

    try {
        const SPLAT = await import('gsplat');

        const renderer = new SPLAT.WebGLRenderer(canvas);
        const scene = new SPLAT.Scene();
        const camera = new SPLAT.Camera();
        const controls = new SPLAT.OrbitControls(camera, canvas);

        await SPLAT.PLYLoader.LoadAsync(fileUrl, scene, (progress) => {
            loadingDiv.textContent = `Loading... ${Math.round(progress * 100)}%`;
        });

        loadingDiv.remove();

        // Animation loop
        let animId;
        const animate = () => {
            animId = requestAnimationFrame(animate);
            controls.update();
            renderer.render(scene, camera);
        };
        animate();

        // Cleanup when container is removed
        const observer = new MutationObserver(() => {
            if (!document.contains(canvas)) {
                cancelAnimationFrame(animId);
                renderer.dispose();
                observer.disconnect();
            }
        });
        observer.observe(document.body, { childList: true, subtree: true });

        // "Set as Preview" button
        if (cogId) {
            const btnContainer = document.createElement('div');
            btnContainer.style.cssText = 'display:flex;justify-content:center;margin-top:8px;';

            const btn = document.createElement('button');
            btn.textContent = 'Set as Preview';
            btn.style.cssText = 'padding:6px 16px;background:#8b5cf6;color:white;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;';
            btn.onclick = async () => {
                btn.textContent = 'Saving...';
                btn.disabled = true;
                renderer.render(scene, camera);
                canvas.toBlob(async (blob) => {
                    if (!blob) { btn.textContent = 'Failed'; return; }
                    const formData = new FormData();
                    formData.append('file', blob, 'thumbnail.png');
                    const resp = await fetch(`${API_BASE_URL}/assets/${cogId}/thumbnail`, {
                        method: 'POST',
                        headers: { Authorization: `Bearer ${token}` },
                        body: formData,
                    });
                    btn.textContent = resp.ok ? 'Saved!' : 'Failed';
                    btn.style.background = resp.ok ? '#10b981' : '#ef4444';
                    if (resp.ok) {
                        thumbnailCache.delete(cogId);
                        thumbnailCache.delete(String(cogId));
                        const gridCell = document.querySelector(`[data-id="${cogId}"] .thumbnail-slot`);
                        if (gridCell) {
                            try {
                                const thumbResp = await fetch(`${API_BASE_URL}/assets/${cogId}/thumbnail`, {
                                    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
                                });
                                if (thumbResp.ok) {
                                    const data = await thumbResp.json();
                                    if (data?.url) applyThumbnail(gridCell, data.url, 'image');
                                }
                            } catch (e) {}
                        }
                    }
                    setTimeout(() => { btn.textContent = 'Set as Preview'; btn.style.background = '#8b5cf6'; btn.disabled = false; }, 2000);
                }, 'image/png');
            };
            btnContainer.appendChild(btn);
            container.appendChild(btnContainer);
        }

    } catch (err) {
        const msg = err.message || String(err);
        if (msg.includes('uchar') || msg.includes('Unsupported property')) {
            loadingDiv.innerHTML = `
                <div style="text-align:center;">
                    <p style="color:#f59e0b;font-weight:600;">Not a Gaussian Splat file</p>
                    <p style="color:#9ca3af;font-size:11px;margin-top:4px;">This PLY file is a regular 3D mesh, not a Gaussian splat.<br>Use "View as 3D Mesh" instead.</p>
                </div>`;
        } else {
            loadingDiv.textContent = `Failed to load splat: ${msg}`;
            loadingDiv.style.color = '#ef4444';
        }
        console.error('Splat preview error:', err);
    }
}

// -----------------------------------------------------------------------
// PDF preview
// -----------------------------------------------------------------------
function renderPDFPreview(container, url, cogId) {
    // Stream PDF through API to avoid MinIO's X-Frame-Options: sameorigin
    const token = sessionStorage.getItem("accessToken");
    const pdfUrl = cogId
        ? `${API_BASE_URL}/assets/${cogId}/pdf-view?token=${encodeURIComponent(token)}`
        : url;
    const iframe = document.createElement('iframe');
    iframe.src = pdfUrl;
    iframe.style.cssText = 'width:100%;height:500px;border:none;border-radius:8px;background:white;';
    container.appendChild(iframe);
}

// -----------------------------------------------------------------------
// DOCX preview — uses the vendored mammoth.browser.min.js to convert .docx
// to HTML, renders it in a sandboxed iframe. No CDN; the library is
// vendored under vendor/mammoth.browser.min.js (sha256 deb07bf230d1...).
// -----------------------------------------------------------------------
// mammoth 1.13.1 (Apache-2.0), vendored at vendor/mammoth.browser.min.js
// (sha256 deb07bf230d1cb3e190bc5adc6743f35c6531b6571d1e5469b24f452a7f0f4ab)
// so the preview works air-gapped / CE offline — no CDN, no network call.
// The file ships in the image via the wholesale `COPY --from=builder /app/vendor
// ./vendor` (Dashboard Dockerfile:85); it is loaded as a plain <script> tag
// exactly like /vendor/chart.js@4.4.0 is loaded in analytics.js, and the UMD
// wrapper publishes it as window.mammoth.
const MAMMOTH_LIB_URL = '/vendor/mammoth.browser.min.js';
const DOCX_MAX_BYTES = 2 * 1024 * 1024;
const DOCX_ALLOWED_TAGS = new Set(['p', 'br', 'hr', 'em', 'strong', 'u', 's', 'sub', 'sup',
    'span', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'table', 'thead',
    'tbody', 'tr', 'td', 'th', 'img', 'a', 'blockquote', 'pre', 'code', 'caption', 'colgroup',
    'col', 'figure', 'figcaption', 'section', 'small', 'mark', 'abbr', 'b', 'i']);
let _mammothPromise = null;

function _ensureMammothLoaded() {
    if (_mammothPromise) return _mammothPromise;
    _mammothPromise = new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.setAttribute('src', MAMMOTH_LIB_URL);
        s.setAttribute('async', 'true');
        s.onload = () => resolve();
        s.onerror = () => reject(new Error('mammoth.js vendor load failed'));
        document.head.appendChild(s);
    });
    return _mammothPromise;
}

function _docxMammoth() {
    if (typeof window !== 'undefined' && window.mammoth) return window.mammoth;
    return null;
}

function _docxNoPreview(container, fileName, note) {
    renderOtherPreview(container, fileName, 'docx');
    if (!note) return;
    const d = document.createElement('div');
    d.style.cssText = 'margin-top:4px;font-size:12px;color:#6b7280;';
    d.textContent = note;
    container.appendChild(d);
}

// String-level sanitizer (no DOM round-trip, so it is deterministic and
// unit-testable): kill whole script blocks, drop dangerous container tags,
// strip inline event handlers, neutralize javascript:/vbscript:/file: URLs,
// then drop every tag that is not on the allow-list (text is kept).
function _sanitizeDocxHtml(html) {
    let out = String(html || '');
    out = out.replace(/<script\b[\s\S]*?(?:<\/script\s*>|$)/gi, '');
    out = out.replace(/<\/?(?:script|iframe|frame|object|embed|link|meta|style|form|base|noscript)\b[^>]*>/gi, '');
    out = out.replace(/<[^>]*>/g, (tag) => tag.replace(/\son[a-z-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, ''));
    out = out.replace(/<[^>]*>/g, (tag) => tag.replace(/\shref\s*=\s*(?:"\s*(?:javascript|vbscript|data|file):[^"]*"|'\s*(?:javascript|vbscript|data|file):[^']*'|(?:javascript|vbscript|data|file):[^\s>]*)/gi, ' href="#"'));
    out = out.replace(/<[^>]*>/g, (tag) => tag.replace(/\ssrc\s*=\s*(?:"\s*(?:javascript|vbscript|file):[^"]*"|'\s*(?:javascript|vbscript|file):[^']*'|(?:javascript|vbscript|file):[^\s>]*)/gi, ' src="#"'));
    out = out.replace(/<\/?([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[^<>]*)?)\/?>/g, (m, name) => DOCX_ALLOWED_TAGS.has(name.toLowerCase()) ? m : '');
    return out;
}

async function renderDocxPreview(container, url, fileName, cogId) {
    let raw = null;
    try {
        const token = sessionStorage.getItem("accessToken");
        const downloadUrl = cogId
            ? `${API_BASE_URL}/assets/${cogId}/download?token=${encodeURIComponent(token)}`
            : url;
        const resp = await fetch(downloadUrl, {
            headers: { Authorization: `Bearer ${token}` },
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        raw = await resp.arrayBuffer();
    } catch (err) {
        _docxNoPreview(container, fileName, 'download failed');
        return;
    }

    if (!raw || !raw.byteLength) {
        _docxNoPreview(container, fileName, 'empty file');
        return;
    }
    if (raw.byteLength > DOCX_MAX_BYTES) {
        _docxNoPreview(container, fileName, 'file larger than 2 MB');
        return;
    }

    try {
        await _ensureMammothLoaded();
    } catch (err) {
        _docxNoPreview(container, fileName, 'mammoth library unavailable');
        return;
    }

    const mammoth = _docxMammoth();
    if (!mammoth || !mammoth.convertToHtml) {
        _docxNoPreview(container, fileName, 'mammoth library unavailable');
        return;
    }

    let html = '';
    try {
        const result = await mammoth.convertToHtml(
            { arrayBuffer: raw },
            { convertImage: mammoth.images.imgElement }
        );
        html = result && result.value ? String(result.value) : '';
    } catch (err) {
        _docxNoPreview(container, fileName, 'could not parse docx');
        return;
    }

    const iframe = document.createElement('iframe');
    iframe.setAttribute('srcdoc', _sanitizeDocxHtml(html));
    iframe.setAttribute('sandbox', 'allow-same-origin');
    iframe.style.cssText = 'width:100%;height:600px;border:none;border-radius:8px;background:#fff;';
    container.appendChild(iframe);
}

// -----------------------------------------------------------------------
// CAD preview — STEP / STP / IGES / IGS / BREP
// -----------------------------------------------------------------------
//
// Strategy (#2 / tasks 15.1–15.3):
//   1. Lazy-load occt-import-js from a CDN on first CAD open. Cached
//      for the session via _cadOcctPromise.
//   2. Fetch the file through the existing presigned-download path,
//      hand the bytes to occt's ReadStepFile / ReadIgesFile / ReadBrepFile,
//      build a Three.js BufferGeometry from the resulting meshes,
//      and feed it into the existing 3D renderer (orbit / pan / zoom).
//   3. On any failure (CDN load, parse error, unsupported variant)
//      fall back to the original graceful panel — server-cached
//      thumbnail check + Download CTA. No silent crashes.
//
// STL / OBJ are NOT routed here; they already render via the 'model3d'
// path through Three.js loaders.

// occt-import-js — pinned BSD-licensed STEP/IGES/BREP parser.
// Vendored locally (see /vendor/occt-import-js@0.0.22) so CAD preview
// works offline/air-gapped; the .wasm sits alongside the .js and is
// located via the library's own locateFile() relative to its script URL.
const OCCT_CDN_URL = "/vendor/occt-import-js@0.0.22/dist/occt-import-js.js";
let _cadOcctPromise = null;

async function _ensureOcctLoaded() {
    if (_cadOcctPromise) return _cadOcctPromise;
    _cadOcctPromise = (async () => {
        // Inject the IIFE script tag (the package exposes a global
        // `occtimportjs` factory). Once loaded, call it to await the
        // WASM bootstrap.
        await new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = OCCT_CDN_URL;
            s.async = true;
            s.onload = resolve;
            s.onerror = () => reject(new Error('occt-import-js CDN load failed'));
            document.head.appendChild(s);
        });
        const factory = window.occtimportjs;
        if (typeof factory !== 'function') {
            throw new Error('occt-import-js global not exposed');
        }
        return factory();   // Promise<occt> — bootstraps the WASM
    })().catch(err => {
        // Reset so a future open can retry on transient CDN failures.
        _cadOcctPromise = null;
        throw err;
    });
    return _cadOcctPromise;
}

async function _runOcctReader(occt, ext, buffer) {
    // occt-import-js dispatches by extension; we pick the right call.
    const u8 = new Uint8Array(buffer);
    if (ext === 'step' || ext === 'stp') return occt.ReadStepFile(u8, null);
    if (ext === 'iges' || ext === 'igs') return occt.ReadIgesFile(u8, null);
    if (ext === 'brep')                  return occt.ReadBrepFile(u8, null);
    throw new Error(`Unsupported CAD extension: ${ext}`);
}

async function _renderCADInBrowser(container, item, ext) {
    // Loading hint — replaces the panel while the WASM downloads + parses.
    container.innerHTML = `
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:300px;gap:12px;color:#6b7280;font-size:13px;">
            <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
            <p>Converting ${ext.toUpperCase()} → mesh… <span style="font-size:11px;">(occt-import-js, first load downloads ~5MB WASM)</span></p>
        </div>
    `;

    const occt = await _ensureOcctLoaded();
    // SLDPRT path (#8 / §5.8) — `renderCADPreview` swaps in a synthetic
    // item carrying the converted-STEP presigned URL via
    // `_convertedStepUrl`. Use it directly so we feed the OCCT reader
    // the converted STEP instead of the raw .sldprt (which it can't read).
    const url = item._convertedStepUrl || await getPresignedUrl(item.id);
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`Source download failed: HTTP ${resp.status}`);
    const buffer = await resp.arrayBuffer();
    const result = await _runOcctReader(occt, ext, buffer);
    if (!result || !result.success || !Array.isArray(result.meshes) || result.meshes.length === 0) {
        throw new Error('parser returned no meshes');
    }

    // Build a single combined BufferGeometry from every mesh in the
    // result. occt returns interleaved positions / indices /
    // (optional) normals as flat Float32 / Uint32 arrays per mesh.
    await ensureThreeLoaded();
    const { THREE, OrbitControls } = threeModules;
    cleanupRenderer();
    // Geometry parsed — drop the loading panel and assemble the viewer.
    container.innerHTML = '';

    const width = container.clientWidth;
    const height = Math.min(width, 400);

    const header = document.createElement('div');
    header.style.cssText = 'font-size:11px;color:#6b7280;display:flex;flex-wrap:wrap;gap:12px;padding:6px 4px;';
    let totalTris = 0;
    result.meshes.forEach(m => {
        const idxLen = (m.attributes && m.attributes.position && m.index)
            ? (m.index.array ? m.index.array.length : 0)
            : (m.attributes?.position?.array?.length / 3) || 0;
        totalTris += Math.floor(idxLen / 3);
    });
    header.innerHTML = `<span>${result.meshes.length} mesh(es) · ${totalTris.toLocaleString()} triangles</span>`;
    container.appendChild(header);

    const canvas = document.createElement('canvas');
    canvas.style.cssText = `width:100%;height:${height}px;border-radius:8px;`;
    container.appendChild(canvas);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x111827);
    const camera = new THREE.PerspectiveCamera(50, width / height, 0.01, 5000);
    camera.position.set(2, 2, 3);
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    activeRenderer = renderer;
    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const dir = new THREE.DirectionalLight(0xffffff, 0.7);
    dir.position.set(5, 10, 7); scene.add(dir);
    const controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;

    const group = new THREE.Group();
    const material = new THREE.MeshStandardMaterial({
        color: 0x88aaff, roughness: 0.6, metalness: 0.1, side: THREE.DoubleSide,
    });

    for (const m of result.meshes) {
        const positions = m.attributes?.position?.array;
        const indices   = m.index?.array;
        const normals   = m.attributes?.normal?.array;
        if (!positions || !indices) continue;
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(positions), 3));
        geo.setIndex(new THREE.BufferAttribute(new Uint32Array(indices), 1));
        if (normals) {
            geo.setAttribute('normal', new THREE.BufferAttribute(new Float32Array(normals), 3));
        } else {
            geo.computeVertexNormals();
        }
        group.add(new THREE.Mesh(geo, material));
    }

    // Center + scale to a unit-ish box so OrbitControls feels right.
    const box = new THREE.Box3().setFromObject(group);
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    group.position.sub(center);
    const scale = 2 / maxDim;
    group.scale.setScalar(scale);
    scene.add(group);

    function animate() {
        activeAnimationId = requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }
    animate();

    new ResizeObserver(() => {
        const w = container.clientWidth;
        const h = Math.min(w, 400);
        camera.aspect = w / h; camera.updateProjectionMatrix();
        renderer.setSize(w, h);
    }).observe(container);
}

function _renderCADFallbackPanel(container, item, ext, opts) {
    const reason = opts && opts.reason;
    const token = sessionStorage.getItem("accessToken");
    const reasonHtml = reason
        ? `<p style="color:#b45309;font-size:11px;text-align:center;max-width:380px;">In-browser conversion didn't work for this file (${escapeHtmlAttr(reason)}). Use the cached thumbnail or download the original.</p>`
        : '';
    container.innerHTML = `
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:300px;gap:14px;padding:0 16px;">
            <div style="font-size:48px;line-height:1;">📐</div>
            <p style="color:#374151;font-size:14px;font-weight:600;">${ext.toUpperCase()} CAD file</p>
            ${reasonHtml || `
            <p style="color:#6b7280;font-size:12px;text-align:center;max-width:380px;line-height:1.5;">
                Open the file in your CAD tool or use the cached thumbnail (if a workflow has converted it).
            </p>`}
            <div style="display:flex;gap:8px;">
                <button id="cad-check-thumb" style="padding:8px 18px;background:hsl(var(--primary));color:white;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;">
                    Check for 3D preview
                </button>
                <button id="cad-download" style="padding:8px 18px;background:#ffffff;color:#374151;border:1px solid #d1d5db;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;">
                    Download original
                </button>
            </div>
        </div>
    `;

    document.getElementById('cad-download').onclick = async () => {
        try {
            const resp = await fetch(`${API_BASE_URL}/assets/${item.id}/download`, {
                headers: { Authorization: `Bearer ${token}` },
            });
            if (!resp.ok) return;
            const data = await resp.json();
            if (data?.download_url) {
                const a = document.createElement('a');
                a.href = data.download_url;
                a.download = data.filename || item.name;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            }
        } catch { /* non-fatal */ }
    };

    document.getElementById('cad-check-thumb').onclick = async () => {
        const btn = document.getElementById('cad-check-thumb');
        btn.textContent = 'Checking...';
        try {
            const resp = await fetch(`${API_BASE_URL}/assets/${item.id}/thumbnail`, {
                headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
            });
            if (resp.ok) {
                const data = await resp.json();
                if (data?.url) {
                    container.innerHTML = '';
                    renderImagePreview(container, data.url, item.name);
                    return;
                }
            }
            btn.textContent = 'No preview yet — run in a workflow to convert';
            btn.disabled = true;
            btn.style.background = '#4b5563';
        } catch {
            btn.textContent = 'No preview available';
            btn.disabled = true;
        }
    };
}

function escapeHtmlAttr(s) {
    return String(s == null ? '' : s).replace(/[<>&"']/g, c => ({
        '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

async function renderCADPreview(container, item) {
    const ext = resolveExt(item).toLowerCase();
    // SLDPRT path (#8 / §5.8): occt-import-js can't read SolidWorks Part
    // files, so the API converts them to STEP on upload. We
    // fetch /cogs/{id}/converted-step which returns one of:
    //   200 + {status:'ready', url} → presigned STEP, render normally
    //   202 + {status:'pending'}    → conversion still running
    //   422 + {status:'failed', error_message}
    //   404                          → no conversion record (legacy
    //                                  SLDPRT uploaded before §5.8)
    if (ext === 'sldprt') {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        let resp;
        try {
            resp = await fetch(`${apiUrl}/assets/${item.id}/converted-step`, {
                headers: { 'Authorization': `Bearer ${token}` },
            });
        } catch (err) {
            _renderCADFallbackPanel(container, item, ext, {reason: 'Network error fetching converted STEP'});
            return;
        }
        if (resp.status === 202) {
            _renderSLDPRTPendingPanel(container, item);
            return;
        }
        if (resp.status === 422) {
            let body = {};
            try { body = await resp.json(); } catch {}
            _renderCADFallbackPanel(container, item, ext, {
                reason: body.error_message || 'SLDPRT conversion failed',
            });
            return;
        }
        if (!resp.ok) {
            _renderCADFallbackPanel(container, item, ext, {
                reason: 'No SLDPRT conversion available — re-upload to trigger conversion.',
            });
            return;
        }
        const body = await resp.json();
        const stepUrl = body.url;
        // Synthesize a STEP-flavoured item so _renderCADInBrowser uses
        // the existing OCCT pipeline. We pass the presigned URL straight
        // through; the in-browser reader fetches + triangulates as
        // usual, then hands the mesh to the same Three.js viewer that
        // renders OBJ/FBX/GLB (#11 unified viewer).
        const stepItem = { ...item, _convertedStepUrl: stepUrl };
        try {
            await _renderCADInBrowser(container, stepItem, 'step');
        } catch (err) {
            _renderCADFallbackPanel(container, item, ext, {reason: err.message || String(err)});
        }
        return;
    }
    try {
        await _renderCADInBrowser(container, item, ext);
    } catch (err) {
        console.warn('CAD in-browser conversion failed; falling back:', err);
        _renderCADFallbackPanel(container, item, ext, {reason: err.message || String(err)});
    }
}

// Point-cloud "converting…" panel (#7 / §5). Auto-polls
// /converted-pointcloud every 5s; re-runs renderPointCloudPreview on
// state change so ready / failed UX surfaces without manual refresh.
function _renderPointCloudPendingPanel(container, cogId, ext) {
    container.innerHTML = `
        <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:300px; padding:24px; color:#444;">
            <div style="width:42px; height:42px; border:4px solid #e2e8f0; border-top-color:#6366f1; border-radius:50%; animation: spin 0.9s linear infinite;"></div>
            <div style="margin-top:16px; font-size:14px; font-weight:600; color:#1e293b;">Converting your point cloud…</div>
            <div style="margin-top:6px; font-size:12px; color:#64748b; max-width:380px; text-align:center;">
                Converting for fast visualization. This usually takes 10–60 seconds.
            </div>
            <style>@keyframes spin { to { transform: rotate(360deg); } }</style>
        </div>
    `;
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem('accessToken');
    let attempts = 0;
    const poll = async () => {
        attempts++;
        if (attempts > 60) return;
        try {
            const r = await fetch(`${apiUrl}/assets/${cogId}/converted-pointcloud`, {
                headers: { 'Authorization': `Bearer ${token}` },
            });
            if (r.status === 202) {
                setTimeout(poll, 5000);
                return;
            }
            // Re-enter the renderer; state has flipped (ready or failed).
            await renderPointCloudPreview(container, '', ext, cogId);
        } catch {
            setTimeout(poll, 5000);
        }
    };
    setTimeout(poll, 5000);
}

// SLDPRT-specific "converting…" panel (#8 / §5.8). Polls the converted-
// step endpoint every 5s; auto-rerenders when ready or failed.
function _renderSLDPRTPendingPanel(container, item) {
    container.innerHTML = `
        <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:300px; padding:24px; color:#444;">
            <div style="width:42px; height:42px; border:4px solid #e2e8f0; border-top-color:#6366f1; border-radius:50%; animation: spin 0.9s linear infinite;"></div>
            <div style="margin-top:16px; font-size:14px; font-weight:600; color:#1e293b;">Converting your CAD file…</div>
            <div style="margin-top:6px; font-size:12px; color:#64748b; max-width:380px; text-align:center;">
                Converting for fast visualization. This usually takes 10–60 seconds.
            </div>
            <style>@keyframes spin { to { transform: rotate(360deg); } }</style>
        </div>
    `;
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem('accessToken');
    let attempts = 0;
    const poll = async () => {
        attempts++;
        if (attempts > 60) return; // 5min cap
        try {
            const r = await fetch(`${apiUrl}/assets/${item.id}/converted-step`, {
                headers: { 'Authorization': `Bearer ${token}` },
            });
            if (r.status === 202) {
                setTimeout(poll, 5000);
                return;
            }
            // Re-render whatever the final state is.
            await renderCADPreview(container, item);
        } catch {
            setTimeout(poll, 5000);
        }
    };
    setTimeout(poll, 5000);
}

// -----------------------------------------------------------------------
// HDR/EXR preview (needs server-side tone mapping)
// -----------------------------------------------------------------------
async function renderHDRPreview(container, item) {
    const ext = resolveExt(item).toUpperCase();
    const token = sessionStorage.getItem("accessToken");

    // First check if a tone-mapped thumbnail already exists
    try {
        const resp = await fetch(`${API_BASE_URL}/assets/${item.id}/thumbnail`, {
            headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
        });
        if (resp.ok) {
            const data = await resp.json();
            if (data?.url) {
                renderImagePreview(container, data.url, item.name + ' (tone-mapped)');
                return;
            }
        }
    } catch (e) { /* no thumbnail */ }

    // No thumbnail — show info card
    container.innerHTML = `
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:300px;gap:16px;">
            <div style="font-size:48px;">🌅</div>
            <p style="color:#e5e7eb;font-size:14px;font-weight:500;">${ext} High Dynamic Range Image</p>
            <p style="color:#9ca3af;font-size:12px;text-align:center;max-width:350px;">
                HDR/EXR files have a wider dynamic range than standard displays can show.
                When used in a workflow, the system will tone-map it to a viewable format automatically.
            </p>
        </div>
    `;
}

// -----------------------------------------------------------------------
// Audio preview
// -----------------------------------------------------------------------
function renderAudioPreview(container, url, filename) {
    container.innerHTML = `
        <div style="display:flex;flex-direction:column;align-items:center;gap:16px;padding:32px 16px;">
            <div style="font-size:48px;">🎵</div>
            <p style="color:#e5e7eb;font-size:14px;font-weight:500;">${filename || 'Audio file'}</p>
            <audio controls src="${url}" style="width:100%;max-width:400px;" preload="metadata">
                Your browser does not support the audio element.
            </audio>
            <canvas id="audio-waveform" width="400" height="80" style="width:100%;max-width:400px;height:80px;border-radius:8px;background:#1f2937;"></canvas>
        </div>
    `;

    // Draw waveform from audio data
    const audioEl = container.querySelector('audio');
    const canvas = container.querySelector('#audio-waveform');
    if (audioEl) {
        // Capture duration for the Details tab once metadata is parsed.
        audioEl.addEventListener('loadedmetadata', () => {
            _mediaInfo.duration_s = Number.isFinite(audioEl.duration) ? audioEl.duration : null;
            populateDetailsTab();
        }, { once: true });
    }
    if (audioEl && canvas) {
        audioEl.addEventListener('canplay', () => {
            try {
                const ctx = new AudioContext();
                fetch(url)
                    .then(r => r.arrayBuffer())
                    .then(buf => ctx.decodeAudioData(buf))
                    .then(audioBuffer => {
                        const data = audioBuffer.getChannelData(0);
                        const c = canvas.getContext('2d');
                        const w = canvas.width;
                        const h = canvas.height;
                        const step = Math.ceil(data.length / w);

                        c.clearRect(0, 0, w, h);
                        c.fillStyle = '#1f2937';
                        c.fillRect(0, 0, w, h);

                        c.strokeStyle = '#6366f1';
                        c.lineWidth = 1;
                        c.beginPath();
                        for (let i = 0; i < w; i++) {
                            let min = 1, max = -1;
                            for (let j = 0; j < step; j++) {
                                const val = data[i * step + j] || 0;
                                if (val < min) min = val;
                                if (val > max) max = val;
                            }
                            const yMin = ((1 + min) / 2) * h;
                            const yMax = ((1 + max) / 2) * h;
                            c.moveTo(i, yMin);
                            c.lineTo(i, yMax);
                        }
                        c.stroke();
                    })
                    .catch(() => {
                        // Waveform failed — just show the audio player
                        canvas.style.display = 'none';
                    });
            } catch (e) {
                canvas.style.display = 'none';
            }
        }, { once: true });
    }
}

// -----------------------------------------------------------------------
// Font preview
// -----------------------------------------------------------------------
function renderFontPreview(container, url, filename) {
    const fontName = `preview-font-${Date.now()}`;
    const style = document.createElement('style');
    style.textContent = `@font-face { font-family: '${fontName}'; src: url('${url}'); }`;
    document.head.appendChild(style);

    const samples = [
        { text: 'ABCDEFGHIJKLM', size: '28px' },
        { text: 'NOPQRSTUVWXYZ', size: '28px' },
        { text: 'abcdefghijklmnopqrstuvwxyz', size: '22px' },
        { text: '0123456789', size: '24px' },
        { text: 'The quick brown fox jumps over the lazy dog.', size: '18px' },
        { text: '!@#$%^&*()_+-=[]{}|;:\'",.<>?/', size: '16px' },
    ];

    container.innerHTML = `
        <div style="padding:24px;background:#1a1a2e;border-radius:8px;">
            <p style="color:#6b7280;font-size:11px;margin-bottom:16px;">${filename}</p>
            ${samples.map(s => `
                <p style="font-family:'${fontName}',sans-serif;font-size:${s.size};color:#e5e7eb;margin:8px 0;line-height:1.4;word-break:break-all;">
                    ${s.text}
                </p>
            `).join('')}
        </div>
    `;
}

// -----------------------------------------------------------------------
// Archive preview (show file listing)
// -----------------------------------------------------------------------
function renderArchivePreview(container, filename, ext) {
    container.innerHTML = `
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:300px;gap:16px;">
            <div style="font-size:48px;">📦</div>
            <p style="color:#e5e7eb;font-size:14px;font-weight:500;">${filename}</p>
            <p style="color:#9ca3af;font-size:12px;">${ext.toUpperCase()} Archive</p>
            <p style="color:#6b7280;font-size:11px;text-align:center;max-width:300px;">
                Archive contents cannot be previewed directly.
                Download the file to extract and view its contents.
            </p>
        </div>
    `;
}

// -----------------------------------------------------------------------
// Generic file preview (non-previewable)
// -----------------------------------------------------------------------
function renderOtherPreview(container, filename, ext) {
    container.innerHTML = `
        <div class="flex flex-col items-center justify-center py-12 text-gray-400">
            <svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/>
            </svg>
            <p class="mt-4 text-sm font-medium text-gray-500">${ext.toUpperCase()} File</p>
            <p class="mt-1 text-xs text-gray-400">No preview available</p>
        </div>
    `;
}

// -----------------------------------------------------------------------
// Main: open preview panel for a given cog/file item
// -----------------------------------------------------------------------
export async function openPreview(item) {
    const panel = document.getElementById('preview-panel');
    const content = document.getElementById('preview-content');
    const metadata = document.getElementById('preview-metadata');
    const filenameEl = document.getElementById('preview-filename');

    if (!panel || !content) return;

    // Don't re-open same file
    if (currentPreviewCogId === item.id) return;
    currentPreviewCogId = item.id;
    _currentItem = item;
    _resetMediaInfo();
    // FX.79 p3a — CUI/ITAR marking banner on this asset (silent: a failed
    // fetch or an unmarked asset simply leaves the banner empty).
    loadAssetMarkingBanner(item.id, () => currentPreviewCogId === item.id,
        // FX.163 p3b4a — the banner's "Clear" button is tenant-admin / superadmin
        // only; the verdict is resolved from the cached /me (silent → false).
        await _canClearMarking());
    // Seed the Details tab with what we know synchronously; IMG/VIDEO
    // load callbacks will refresh with dimensions/duration once
    // available.
    populateDetailsTab();

    // Show active preview content, hide placeholder
    const placeholder = document.getElementById('preview-placeholder');
    const active = document.getElementById('preview-active');
    if (placeholder) placeholder.classList.add('hidden');
    if (active) active.classList.remove('hidden');

    // Set filename
    filenameEl.textContent = item.name || 'Preview';

    // Loading state
    cleanupRenderer();
    content.innerHTML = `
        <div class="flex items-center justify-center h-48 text-gray-400">
            <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
        </div>
    `;

    // Metadata — resolveExt prefers backend-supplied authoritative
    // file_extension/mime_type so the visualizer keeps working after a
    // rename that strips or changes the extension on the display name.
    const ext = resolveExt(item);
    const category = categorize(ext);
    metadata.innerHTML = `
        <div class="flex justify-between"><span class="text-gray-500">Name</span><span class="font-medium text-gray-700 truncate ml-2 text-right">${item.name}</span></div>
        <div class="flex justify-between"><span class="text-gray-500">Type</span><span class="font-medium text-gray-700">${ext.toUpperCase() || item.cog_type}</span></div>
        ${item.size_bytes ? `<div class="flex justify-between"><span class="text-gray-500">Size</span><span class="font-medium text-gray-700">${formatBytes(item.size_bytes)}</span></div>` : ''}
        <div class="flex justify-between"><span class="text-gray-500">Category</span><span class="font-medium text-gray-700 capitalize">${category === 'model3d' ? '3D Model' : category}</span></div>
    `;

    // For folders, attempt frame-sequence detection (#19,
    // launch-day-followup-2). If the folder's children look like a
    // numbered image sequence (frame-001.png, frame-002.png, …),
    // render a sequence-detected card with a frame count + a thumbnail
    // of the first frame so the user knows the system saw the
    // pattern. Server-side ffmpeg-concat → video preview is the
    // follow-up; this lands the detection so empty "no preview"
    // panels stop appearing on obvious sequence folders.
    if (item.cog_type && item.cog_type.includes('folder')) {
        let sequence = null;
        try {
            sequence = await detectFolderFrameSequence(item.id);
        } catch (e) {
            console.debug('Folder sequence detection failed (non-fatal):', e);
        }
        if (sequence && sequence.frame_count >= 2) {
            const ext = sequence.extension || 'frames';
            content.innerHTML = `
                <div class="flex flex-col items-center justify-center py-8 text-gray-700">
                    <div class="relative mb-4">
                        <img id="frame-seq-thumb" alt="First frame" class="max-w-[420px] max-h-[280px] rounded-md border border-gray-200 shadow-sm" />
                        <div class="absolute inset-0 flex items-center justify-center pointer-events-none">
                            <div class="bg-black/45 rounded-full w-16 h-16 flex items-center justify-center">
                                <svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 24 24" fill="white">
                                    <polygon points="6 4 20 12 6 20" />
                                </svg>
                            </div>
                        </div>
                    </div>
                    <p class="text-base font-semibold">Frame sequence detected</p>
                    <p class="mt-1 text-sm text-gray-600">${sequence.frame_count} frames (.${ext})${sequence.base ? ` — pattern <code class="text-xs bg-gray-100 px-1 rounded">${escapeHtmlAttr(sequence.base)}-NNN.${ext}</code>` : ''}</p>
                    <p class="mt-3 text-xs text-gray-500 text-center max-w-md">Video preview is on the way; for now you can open any single frame to inspect it.</p>
                </div>
            `;
            // Lazy-load the first frame's thumbnail so the card paints
            // immediately and the image streams in.
            try {
                const url = await getPresignedUrl(sequence.first_frame_cog_id);
                const img = document.getElementById('frame-seq-thumb');
                if (img && currentPreviewCogId === item.id) img.src = url;
            } catch { /* leave thumbnail blank — card still useful */ }
            return;
        }

        content.innerHTML = `
            <div class="flex flex-col items-center justify-center py-12 text-gray-400">
                <svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                </svg>
                <p class="mt-4 text-sm font-medium text-gray-500">Folder</p>
                <p class="mt-1 text-xs text-gray-400">Double-click to open</p>
            </div>
        `;
        return;
    }

    // Get presigned URL
    try {
        const url = await getPresignedUrl(item.id);

        // Check we're still viewing the same item
        if (currentPreviewCogId !== item.id) return;

        content.innerHTML = '';

        switch (category) {
            case 'image':
                if (BROWSER_IMAGE_EXTS.has(ext)) {
                    // Try image-sequence detection first. The detector
                    // hits the API to enumerate siblings; on no match
                    // (single image, or the file isn't part of a
                    // numbered sequence) it returns null and we fall
                    // through to the still-image preview path.
                    let sequence = null;
                    try {
                        sequence = await detectImageSequence(item);
                    } catch (e) {
                        console.debug('Sequence detection failed (non-fatal):', e);
                    }
                    if (currentPreviewCogId !== item.id) return;
                    if (sequence) {
                        await renderImageSequencePreview(content, sequence, item.id);
                    } else {
                        renderImagePreview(content, url, item.name, item.id);
                    }
                } else {
                    renderOtherPreview(content, item.name, ext);
                }
                break;
            case 'usd':
                renderUSDPreview(content, url, ext, item.id);
                break;
            case 'model3d':
                if (false) { // USD now handled by 'usd' category above
                    break;
                }
                if (ext === 'ply') {
                    // PLY can be mesh or Gaussian splat — let user choose
                    renderPLYChooser(content, url, ext, item.id);
                } else if (THREEJS_EXTS.has(ext)) {
                    await render3DPreview(content, url, ext, item.id, false, true);
                } else {
                    renderOtherPreview(content, item.name, ext);
                }
                break;
            case 'video':
                renderVideoPreview(content, url);
                break;
            case 'text':
                await renderTextPreview(content, url, item.name, ext, item.id);
                break;
            case 'pdf':
                await renderPDFPreview(content, url, item.id);
                break;
            case 'docx':
                await renderDocxPreview(content, url, item.name, item.id);
                break;
            case 'splat':
                renderSplatPreview(content, url, item.id, item.name);
                break;
            case 'pointcloud':
                await renderPointCloudPreview(content, url, ext, item.id);
                break;
            case 'audio':
                renderAudioPreview(content, url, item.name);
                break;
            case 'cad':
                renderCADPreview(content, item);
                break;
            case 'hdr':
                renderHDRPreview(content, item);
                break;
            case 'font':
                renderFontPreview(content, url, item.name);
                break;
            case 'archive':
                renderArchivePreview(content, item.name, ext);
                break;
            default:
                renderOtherPreview(content, item.name, ext);
        }
    } catch (error) {
        console.error('Preview error:', error);
        if (currentPreviewCogId === item.id) {
            content.innerHTML = `<div class="text-center text-red-500 py-8 text-sm">Failed to load preview</div>`;
        }
    }
}

// -----------------------------------------------------------------------
// Close preview panel
// -----------------------------------------------------------------------
export function closePreview() {
    // Hide active preview, show placeholder — panel stays visible.
    // Also collapse any expanded-state and remove its backdrop so X
    // works as a single "exit" regardless of layout.
    const panel = document.getElementById('preview-panel');
    if (panel && panel.classList.contains('preview-panel-expanded')) {
        panel.classList.remove('preview-panel-expanded');
        const backdrop = document.getElementById('preview-panel-backdrop');
        if (backdrop) backdrop.remove();
    }
    const placeholder = document.getElementById('preview-placeholder');
    const active = document.getElementById('preview-active');
    if (placeholder) placeholder.classList.remove('hidden');
    if (active) active.classList.add('hidden');
    clearAssetMarkingBanner();
    cleanupRenderer();
    currentPreviewCogId = null;
}

// -----------------------------------------------------------------------
// Inline thumbnail for file cards (small static preview)
// -----------------------------------------------------------------------

// Cache of generated thumbnail data URLs to avoid re-fetching
const thumbnailCache = new Map();

// Lazy-loaded PDF.js for rendering the first page of a PDF as a
// grid-thumbnail image. Vendored locally (see /vendor/pdfjs-dist@4.7.76)
// so it works offline/air-gapped; same pattern as THREE.js. The promise
// is memoised so a grid full of PDFs only triggers one ESM import.
//
// 4.x is the current major; pinning the patch version keeps the
// dashboard reproducible from one image rebuild to the next.
let _pdfjsPromise = null;
function _loadPdfJs() {
    if (_pdfjsPromise) return _pdfjsPromise;
    const PDFJS_VERSION = "4.7.76";
    _pdfjsPromise = (async () => {
        const lib = await import(
            `/vendor/pdfjs-dist@${PDFJS_VERSION}/build/pdf.min.mjs`
        );
        lib.GlobalWorkerOptions.workerSrc =
            `/vendor/pdfjs-dist@${PDFJS_VERSION}/build/pdf.worker.min.mjs`;
        return lib;
    })().catch((err) => {
        // Reset so a later thumbnail call retries (likely a transient
        // CDN blip). Returning null sends the caller down the emoji
        // fallback path below.
        _pdfjsPromise = null;
        console.warn("pdf.js load failed:", err);
        return null;
    });
    return _pdfjsPromise;
}

// Render page 1 of a tenant PDF to a PNG data URL for the grid card.
// Fetches via /cogs/{id}/pdf-view (already wired for the preview pane,
// streams through the API so no MinIO X-Frame-Options / signed-URL
// considerations). Returns null on any failure — caller falls back to
// the static PDF icon thumbnail.
async function _renderPdfThumbnail(cogId) {
    const pdfjs = await _loadPdfJs();
    if (!pdfjs) return null;
    const token = sessionStorage.getItem("accessToken");
    if (!token) return null;
    try {
        const resp = await fetch(
            `${API_BASE_URL}/assets/${cogId}/pdf-view?token=${encodeURIComponent(token)}`
        );
        if (!resp.ok) return null;
        const buf = await resp.arrayBuffer();
        const doc = await pdfjs.getDocument({ data: buf }).promise;
        const page = await doc.getPage(1);
        const viewport = page.getViewport({ scale: 1 });
        // Target ~320 CSS px wide so the canvas looks crisp at retina
        // densities; the grid cell scales it down via CSS to ~120-160
        // CSS px. PDF aspect ratios vary wildly (US Letter portrait vs
        // wide landscape) — scale from width so the height falls out.
        const targetWidth = 320;
        const scale = targetWidth / viewport.width;
        const scaledViewport = page.getViewport({ scale });
        const canvas = document.createElement("canvas");
        canvas.width = Math.ceil(scaledViewport.width);
        canvas.height = Math.ceil(scaledViewport.height);
        const ctx = canvas.getContext("2d");
        // PDFs render with transparent background by default; fill
        // white so dark themes / transparent canvases don't show
        // text-on-nothing.
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        await page.render({ canvasContext: ctx, viewport: scaledViewport }).promise;
        return canvas.toDataURL("image/png");
    } catch (err) {
        console.debug("pdf thumbnail render failed for cog", cogId, err);
        return null;
    }
}

export function clearThumbnailCache() {
    thumbnailCache.clear();
}

/**
 * Load a small inline thumbnail into a container element.
 * For images: loads a small <img>.
 * For 3D models: renders a static snapshot via offscreen Three.js.
 * For video: loads a poster frame.
 * Returns quickly — rendering happens async.
 */
export async function loadThumbnail(container, item) {
    const ext = resolveExt(item);
    const category = categorize(ext);

    if (category === 'other') return; // No thumbnail for unknown types
    if (item.cog_type && item.cog_type.includes('folder')) return; // No thumbnail for folders
    // Categories that don't render a visual tile in the file grid —
    // the FE applies a generic icon-based card instead. Hitting
    // /download / /thumbnail for these adds nothing but spam (and 404s
    // for any orphan-content cog like Tim's broken `file_hashes.txt`).
    if (category === 'audio' || category === 'font' || category === 'archive') {
        return;
    }

    // Bug-8: text/code files get a first-lines snippet preview in the grid (they
    // already render in the preview panel). Handled before the presigned-URL
    // fetch — text uses /text-content, not a thumbnail object.
    if (category === 'text') {
        if (thumbnailCache.has(item.id)) {
            applyThumbnail(container, thumbnailCache.get(item.id), 'code');
            return;
        }
        try {
            const token = sessionStorage.getItem("accessToken");
            const r = await fetch(`${API_BASE_URL}/assets/${item.id}/text-content`, {
                headers: { Authorization: `Bearer ${token}` },
            });
            if (r.ok) {
                const raw = await r.text();
                let text = raw;
                try { const j = JSON.parse(raw); if (j && typeof j.content === 'string') text = j.content; } catch {}
                const snippet = _escapeHtml(text.split('\n').slice(0, 14).join('\n'));
                const html = `<pre style="margin:0;padding:6px;width:100%;height:80px;overflow:hidden;`
                    + `font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:8px;line-height:1.25;`
                    + `color:#374151;background:#f9fafb;white-space:pre;text-align:left;border-radius:0.75rem 0.75rem 0 0;">${snippet}</pre>`;
                thumbnailCache.set(item.id, html);
                applyThumbnail(container, html, 'code');
            }
        } catch (e) { /* leave the generic icon */ }
        return;
    }

    // Check cache
    if (thumbnailCache.has(item.id)) {
        applyThumbnail(container, thumbnailCache.get(item.id), category);
        return;
    }

    try {
        const url = await getPresignedUrl(item.id);

        if (category === 'image' && BROWSER_IMAGE_EXTS.has(ext)) {
            thumbnailCache.set(item.id, url);
            applyThumbnail(container, url, 'image');
        } else if (category === 'video') {
            thumbnailCache.set(item.id, url);
            applyThumbnail(container, url, 'video');
        } else if (category === 'font') {
            // Load font and render a sample letter as thumbnail
            const fontName = `thumb-font-${item.id}`;
            const style = document.createElement('style');
            style.textContent = `@font-face { font-family: '${fontName}'; src: url('${url}'); }`;
            document.head.appendChild(style);
            const html = `<div style="width:100%;height:80px;background:#1a1a2e;border-radius:4px;display:flex;align-items:center;justify-content:center;font-family:'${fontName}',serif;font-size:36px;color:#e5e7eb;">Aa</div>`;
            thumbnailCache.set(item.id, html);
            container.innerHTML = html;
        } else if (category === 'archive') {
            const html = `<div style="width:100%;height:80px;background:#1c1917;border-radius:4px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;">
                <span style="font-size:24px;line-height:1;">📦</span>
                <span style="font-size:9px;color:#a3a3a3;font-weight:600;">${ext.toUpperCase()}</span>
            </div>`;
            thumbnailCache.set(item.id, html);
            container.innerHTML = html;
        } else if (category === 'hdr') {
            // Check for server thumbnail first
            try {
                const token = sessionStorage.getItem("accessToken");
                const thumbResp = await fetch(`${API_BASE_URL}/assets/${item.id}/thumbnail`, {
                    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
                });
                if (thumbResp.ok) {
                    const thumbData = await thumbResp.json();
                    if (thumbData?.url) {
                        thumbnailCache.set(item.id, thumbData.url);
                        applyThumbnail(container, thumbData.url, 'image');
                        return;
                    }
                }
            } catch (e) { /* no thumbnail */ }

            const html = `<div style="width:100%;height:80px;background:#1e1b4b;border-radius:4px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;">
                <span style="font-size:24px;line-height:1;">🌅</span>
                <span style="font-size:9px;color:#f59e0b;font-weight:600;">${ext.toUpperCase()}</span>
            </div>`;
            thumbnailCache.set(item.id, html);
            container.innerHTML = html;
        } else if (category === 'usd') {
            // USD files — check for server-generated thumbnail
            try {
                const token = sessionStorage.getItem("accessToken");
                const thumbResp = await fetch(`${API_BASE_URL}/assets/${item.id}/thumbnail`, {
                    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
                });
                if (thumbResp.ok) {
                    const thumbData = await thumbResp.json();
                    if (thumbData?.url) {
                        thumbnailCache.set(item.id, thumbData.url);
                        applyThumbnail(container, thumbData.url, 'image');
                        return;
                    }
                }
            } catch (e) { /* no thumbnail yet */ }

            const html = `<div style="width:100%;height:80px;background:#1a1a2e;border-radius:4px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;">
                <span style="font-size:24px;line-height:1;">🎲</span>
                <span style="font-size:9px;color:#a78bfa;font-weight:600;">${ext.toUpperCase()}</span>
                <span style="font-size:7px;color:#9ca3af;">Generating...</span>
            </div>`;
            thumbnailCache.set(item.id, html);
            container.innerHTML = html;
        } else if (category === 'cad') {
            // Check for server-generated thumbnail (auto-dispatched on upload)
            try {
                const token = sessionStorage.getItem("accessToken");
                const thumbResp = await fetch(`${API_BASE_URL}/assets/${item.id}/thumbnail`, {
                    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
                });
                if (thumbResp.ok) {
                    const thumbData = await thumbResp.json();
                    if (thumbData?.url) {
                        thumbnailCache.set(item.id, thumbData.url);
                        applyThumbnail(container, thumbData.url, 'image');
                        return;
                    }
                }
            } catch (e) { /* no thumbnail yet */ }

            const html = `<div style="width:100%;height:80px;background:#1c1917;border-radius:4px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;">
                <span style="font-size:24px;line-height:1;">⚙️</span>
                <span style="font-size:9px;color:#f59e0b;font-weight:600;">${ext.toUpperCase()}</span>
                <span style="font-size:7px;color:#a8a29e;">Generating preview...</span>
            </div>`;
            thumbnailCache.set(item.id, html);
            container.innerHTML = html;
        } else if (category === 'audio') {
            const html = `<div style="width:100%;height:80px;background:#1e1b4b;border-radius:4px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;">
                <span style="font-size:24px;line-height:1;">🎵</span>
                <span style="font-size:9px;color:#a78bfa;font-weight:600;">${ext.toUpperCase()}</span>
            </div>`;
            thumbnailCache.set(item.id, html);
            container.innerHTML = html;
        } else if (category === 'pointcloud') {
            const html = `<div style="width:100%;height:80px;background:#0c1222;border-radius:4px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;">
                <span style="font-size:24px;line-height:1;">✦</span>
                <span style="font-size:9px;color:#60a5fa;font-weight:600;">${ext.toUpperCase()}</span>
            </div>`;
            thumbnailCache.set(item.id, html);
            container.innerHTML = html;
        } else if (category === 'splat') {
            const html = `<div style="width:100%;height:80px;background:#0f172a;border-radius:4px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;">
                <span style="font-size:24px;line-height:1;">✨</span>
                <span style="font-size:9px;color:#a78bfa;font-weight:600;">SPLAT</span>
            </div>`;
            thumbnailCache.set(item.id, html);
            container.innerHTML = html;
        } else if (category === 'pdf') {
            // Render page 1 of the PDF as the thumbnail (matches the
            // behaviour for images / video / 3D — see _renderPdfThumbnail
            // above). Falls back to a static red PDF icon if pdf.js
            // fails to load, the fetch errors, or the document is
            // unparseable. Cached per cog id so flipping between grid
            // and list views doesn't re-fetch the file.
            const fallback = `<div style="width:100%;height:80px;background:#fef2f2;border-radius:4px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;">
                <span style="font-size:28px;line-height:1;">📕</span>
                <span style="font-size:9px;color:#dc2626;font-weight:600;">PDF</span>
            </div>`;
            try {
                const dataUrl = await _renderPdfThumbnail(item.id);
                if (dataUrl) {
                    const html = `<img src="${dataUrl}" alt="${item.name}" style="width:100%;height:80px;object-fit:cover;object-position:top;border-radius:4px;background:#ffffff;">`;
                    thumbnailCache.set(item.id, html);
                    container.innerHTML = html;
                } else {
                    thumbnailCache.set(item.id, fallback);
                    container.innerHTML = fallback;
                }
            } catch (e) {
                thumbnailCache.set(item.id, fallback);
                container.innerHTML = fallback;
            }
        } else if (category === 'text') {
            // Show a mini code preview as thumbnail
            try {
                const token = sessionStorage.getItem("accessToken");
                const resp = await fetch(`${API_BASE_URL}/assets/${item.id}/text-content`, {
                    headers: { Authorization: `Bearer ${token}` },
                });
                if (resp.ok) {
                    let text = await resp.text();
                    text = text.substring(0, 200).replace(/</g, '&lt;').replace(/>/g, '&gt;');
                    const lines = text.split('\n').slice(0, 5).join('\n');
                    const html = `<div style="background:#111827;color:#9ca3af;padding:6px;border-radius:4px;font-size:7px;line-height:1.3;font-family:monospace;overflow:hidden;height:80px;width:100%;">${lines}</div>`;
                    thumbnailCache.set(item.id, html);
                    container.innerHTML = html;
                }
            } catch (e) { /* show default icon */ }
        } else if (category === 'model3d' && THREEJS_EXTS.has(ext)) {
            // Try server-generated thumbnail first (fast, no Three.js load)
            try {
                const token = sessionStorage.getItem("accessToken");
                const thumbResp = await fetch(`${API_BASE_URL}/assets/${item.id}/thumbnail`, {
                    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
                });
                if (thumbResp.ok) {
                    const thumbData = await thumbResp.json();
                    if (thumbData?.url) {
                        thumbnailCache.set(item.id, thumbData.url);
                        applyThumbnail(container, thumbData.url, 'image');
                        return;
                    }
                }
            } catch (e) { /* fall through to Three.js */ }

            // Fallback: render client-side snapshot via Three.js
            const snapshotUrl = await render3DSnapshot(url, ext);
            if (snapshotUrl) {
                thumbnailCache.set(item.id, snapshotUrl);
                applyThumbnail(container, snapshotUrl, 'image');
            }
        }
    } catch (err) {
        // Silently fail — card keeps its default icon
        console.debug('Thumbnail load failed for', item.name, err);
    }
}

function applyThumbnail(container, url, category) {
    if (category === 'image') {
        const img = document.createElement('img');
        img.src = url;
        img.className = 'w-full h-20 object-cover rounded';
        img.onerror = () => img.remove();
        container.innerHTML = '';
        container.appendChild(img);
    } else if (category === 'video') {
        const video = document.createElement('video');
        video.src = url;
        video.className = 'w-full h-20 object-cover rounded';
        video.muted = true;
        video.preload = 'metadata';
        video.playsInline = true;
        // Show first frame as poster
        video.addEventListener('loadeddata', () => {
            video.currentTime = 0.1;
        });
        video.onerror = () => video.remove();
        container.innerHTML = '';
        container.appendChild(video);
    } else if (category === 'code') {
        // Bug-8: `url` is pre-built, already-escaped snippet HTML (a <pre>).
        container.innerHTML = url;
    }
}

/**
 * Render a 3D model to a static image (data URL) using offscreen Three.js.
 */
async function render3DSnapshot(url, ext) {
    await ensureThreeLoaded();
    const { THREE, GLTFLoader, OBJLoader, STLLoader, PLYLoader, FBXLoader, USDZLoader } = threeModules;

    const size = 160;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x2a2a3e);

    const camera = new THREE.PerspectiveCamera(50, 1, 0.01, 100);
    camera.position.set(0, 1, 3);

    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    renderer.setSize(size, size);
    renderer.toneMapping = THREE.ACESFilmicToneMapping;

    const ambient = new THREE.AmbientLight(0xffffff, 0.7);
    scene.add(ambient);
    const dir = new THREE.DirectionalLight(0xffffff, 0.8);
    dir.position.set(3, 4, 5);
    scene.add(dir);

    let loader;
    switch (ext) {
        case 'glb': case 'gltf': loader = new GLTFLoader(); break;
        case 'obj': loader = new OBJLoader(); break;
        case 'stl': loader = new STLLoader(); break;
        case 'ply': loader = new PLYLoader(); break;
        case 'fbx': loader = new FBXLoader(); break;
        case 'usdz': case 'usd': case 'usda': case 'usdc':
            // USD files use server-side GLB conversion — can't snapshot client-side
            renderer.dispose();
            return null;
        default: loader = new GLTFLoader();
    }

    try {
        const result = await new Promise((resolve, reject) => {
            loader.load(url, resolve, undefined, reject);
        });

        let object;
        if (result.scene) {
            object = result.scene;
        } else if (result.isBufferGeometry) {
            result.computeVertexNormals();
            object = new THREE.Mesh(result, new THREE.MeshStandardMaterial({ color: 0x888888, side: THREE.DoubleSide }));
        } else {
            object = result;
        }

        const box = new THREE.Box3().setFromObject(object);
        const center = box.getCenter(new THREE.Vector3());
        const s = box.getSize(new THREE.Vector3());
        const maxDim = Math.max(s.x, s.y, s.z);
        object.position.sub(center);
        object.scale.multiplyScalar(2 / maxDim);
        scene.add(object);

        camera.lookAt(0, 0, 0);
        renderer.render(scene, camera);

        const dataUrl = renderer.domElement.toDataURL('image/png');
        renderer.dispose();
        return dataUrl;
    } catch (err) {
        renderer.dispose();
        console.debug('3D snapshot failed:', err);
        return null;
    }
}

// -----------------------------------------------------------------------
// Initialize preview panel event listeners
// -----------------------------------------------------------------------
export function initPreviewListeners() {
    const closeBtn = document.getElementById('close-preview-button');
    if (closeBtn) {
        closeBtn.addEventListener('click', closePreview);
    }
    const expandBtn = document.getElementById('expand-preview-button');
    if (expandBtn) {
        expandBtn.addEventListener('click', () => {
            const panel = document.getElementById('preview-panel');
            if (!panel) return;
            // Toggle the `expanded` class. CSS does the rest — when
            // expanded, the panel pops out of the sidebar flow into a
            // 92vw × 90vh fixed-position card with a backdrop. Click
            // expand again (or close) to dismiss.
            panel.classList.toggle('preview-panel-expanded');
            const isExpanded = panel.classList.contains('preview-panel-expanded');
            // Manage the body-scoped backdrop element so clicking
            // outside the expanded panel collapses it.
            let backdrop = document.getElementById('preview-panel-backdrop');
            if (isExpanded) {
                if (!backdrop) {
                    backdrop = document.createElement('div');
                    backdrop.id = 'preview-panel-backdrop';
                    backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:40;';
                    backdrop.onclick = () => expandBtn.click();
                    document.body.appendChild(backdrop);
                }
            } else if (backdrop) {
                backdrop.remove();
            }
            // Resize the renderer (if any) to fit the new container size.
            // Three.js uses a ResizeObserver on the container, but the
            // observer fires before the CSS transition finishes — give
            // it a beat then dispatch a synthetic resize.
            setTimeout(() => window.dispatchEvent(new Event('resize')), 320);
        });
    }
}

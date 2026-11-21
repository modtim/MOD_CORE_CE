// remove-minio-versitygw Gate 5 — BYO-S3 Storage Connections page.
//
// Mirrors the encrypted-secret entry pattern (js/secrets.js): a table of the
// tenant's connections with add / test / activate / deactivate / remove. The
// secret key is write-only — it is sent on save and NEVER returned by the API,
// so it is never redisplayed. Activation is gated on a passing connection test.
import { storageConnections, tenantStorage, isPaidEdition } from './api.js';
import { loadModal, closeModal } from './ui.js';

// BYO-S3 activation (owner decisions 10-01, WS4.Q17, 11-09: CE, not gated).
//
// Activation is enabled in every edition. The API still enforces its own
// server-side rules (a passing connection test first; on hosted SaaS also the
// operator flag and the tenant's plan), so this flag is UI-only. Deactivation
// stays available as the recovery path back to the house default.
const BYO_ACTIVATION_ENABLED = true;
const BYO_PREVIEW_MSG = 'BYO-S3 activation is in preview and temporarily disabled while the data plane is finalized. You can save and test a connection now; you\'ll be able to activate it in a later release.';

function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// --- Migration progress poller (WS4.Q17.17) --------------------------------
// Same backoff semantics as wizard-queue.js's BackoffPoller (the Dashboard's
// shared poller pattern — no bare setInterval): exponential backoff capped at
// maxMs, reset to baseMs on success, paused while the tab is hidden (resumed
// on visibility), and self-heals by probing at maxMs instead of hard-stopping
// after repeated failures.
class BackoffPoller {
    constructor(fn, { baseMs = 5000, maxMs = 60000, maxFailures = 5 } = {}) {
        this.fn = fn;
        this.baseMs = baseMs;
        this.maxMs = maxMs;
        this.maxFailures = maxFailures;
        this.timer = null;
        this.curDelay = baseMs;
        this.failures = 0;
        this.enabled = false;
        this.busy = false;
        this._onVisibility = this._onVisibility.bind(this);
    }

    start() {
        if (this.enabled) return;
        this.enabled = true;
        this.failures = 0;
        this.curDelay = this.baseMs;
        document.addEventListener('visibilitychange', this._onVisibility);
        if (document.visibilityState !== 'hidden') this._tick();
    }

    stop() {
        this.enabled = false;
        this._clearTimer();
        document.removeEventListener('visibilitychange', this._onVisibility);
    }

    _clearTimer() {
        if (this.timer) { clearTimeout(this.timer); this.timer = null; }
    }

    _onVisibility() {
        if (!this.enabled) return;
        this._clearTimer();
        if (document.visibilityState !== 'hidden') this._tick();
    }

    _schedule(delay) {
        this._clearTimer();
        if (!this.enabled) return;
        this.timer = setTimeout(() => { this.timer = null; this._tick(); }, delay);
    }

    async _tick() {
        if (!this.enabled || this.busy) return;
        if (document.visibilityState === 'hidden') return;
        this.busy = true;
        try {
            await this.fn();
            this.failures = 0;
            this.curDelay = this.baseMs;
            this._schedule(this.baseMs);
        } catch (err) {
            this.failures += 1;
            this.curDelay = Math.min(this.curDelay * 2, this.maxMs);
            this._schedule(this.failures >= this.maxFailures ? this.maxMs : this.curDelay);
        } finally {
            this.busy = false;
        }
    }
}

// Active migration progress (one at a time per page). _migrationState holds the
// last status object so renderConnections can always draw the progress row,
// even after a full list re-render wipes the in-place row.
let _migrationPoller = null;
let _migrationConnId = null;
let _migrationState = null;

function _fmtElapsed(ms) {
    if (typeof ms !== 'number' || !isFinite(ms) || ms < 0) return '—';
    const s = Math.floor(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function startMigrationProgress(connId) {
    stopMigrationProgress();
    _migrationConnId = connId;
    _migrationState = { migration_status: 'pending', migrated_object_count: 0, migrated_bytes: 0, total_objects: null };
    renderMigrationRow();
    _migrationPoller = new BackoffPoller(() => pollMigrationStatus(connId), { baseMs: 3000, maxMs: 30000 });
    _migrationPoller.start();
}

function stopMigrationProgress() {
    if (_migrationPoller) { _migrationPoller.stop(); _migrationPoller = null; }
}

async function pollMigrationStatus(connId) {
    if (_migrationConnId !== connId) return;
    const st = await storageConnections.migrationStatus(connId);
    _migrationState = st;
    if (st.migration_status === 'verified') {
        // Terminal: stop polling and refresh so the row flips to Active,
        // leaving a "Migration complete" progress row behind.
        stopMigrationProgress();
        _migrationState = st;
        await loadConnections();
        renderMigrationRow();
    } else if (st.migration_status === 'failed') {
        stopMigrationProgress();
        // Refresh first (the row flips back to Inactive), THEN re-append the
        // error row — loadConnections rewrites the whole tbody.
        _migrationState = st;
        await loadConnections();
        renderMigrationRow();
    } else {
        // Still pending/running — update the row in place (no list re-fetch).
        renderMigrationRow();
    }
}

function _migrationRowHtml(st) {
    const failed = st.migration_status === 'failed';
    const verified = st.migration_status === 'verified';
    const total = (typeof st.total_objects === 'number') ? st.total_objects : null;
    const done = st.migrated_object_count || 0;
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : null;
    const startedAt = st.migration_started_at
        ? Date.now() - new Date(st.migration_started_at).getTime()
        : null;
    const objects = total != null ? `${done}/${total} objects` : `${done} objects`;
    let lead, note;
    if (verified && st.skipped_missing === true) {
        const keys = Array.isArray(st.missing_source_keys) ? st.missing_source_keys : [];
        const missing = (typeof st.missing_source_count === 'number') ? st.missing_source_count : keys.length;
        const keyList = keys.length
            ? `<ul class="list-disc ml-5 mt-1 text-xs text-gray-700" id="storage-migration-skipped-keys">${keys.map((k) => `<li><code>${escapeHtml(k)}</code></li>`).join('')}</ul>`
            : '';
        const more = missing > keys.length ? `<div class="text-xs text-gray-500">Showing the first ${keys.length} of ${missing}.</div>` : '';
        lead = `<span class="text-green-600 font-medium">Migration verified</span>`;
        note = `${done} of ${total != null ? total : done + missing} objects copied, ${missing} ${missing === 1 ? 'was' : 'were'} already missing from the old backend (listed below). ${missing === 1 ? 'That file is' : 'Those files are'} no longer readable; the rest of your library is live.${keyList}${more}`;
    } else if (verified) {
        lead = `<span class="text-green-600 font-medium">Migration complete</span>`;
        note = 'Your files now live on this storage connection.';
    } else if (failed) {
        lead = `<span class="text-red-600 font-medium">Migration failed</span>`;
        note = `<span class="text-red-600">${escapeHtml(st.migration_error || 'unknown error')}</span> — <strong>originals untouched</strong>; your files stay on the previous storage.`;
    } else {
        lead = `<span class="text-blue-600 font-medium">${st.migration_status === 'running' ? 'Migrating' : 'Waiting for worker'}…</span>`;
        note = pct != null
            ? `${objects} copied (${pct}%) · ${_fmtBytes(st.migrated_bytes || 0)} · ${_fmtElapsed(startedAt)}`
            : `${objects} copied · ${_fmtBytes(st.migrated_bytes || 0)} · ${_fmtElapsed(startedAt)}`;
    }
    return `
        <tr id="storage-migration-progress-row" class="border-b bg-blue-50/60">
            <td colspan="6" class="py-2 px-4 text-sm">
                ${lead} &nbsp; <span class="text-gray-600">${note}</span>
            </td>
        </tr>`;
}

function renderMigrationRow() {
    if (!_migrationState) return;
    const tbody = document.querySelector('#storage-connections-table tbody');
    if (!tbody) return;
    const existing = document.getElementById('storage-migration-progress-row');
    if (existing) existing.remove();
    // Append the progress row right after the migrating connection's row (or
    // at the end if that row isn't currently rendered).
    const target = tbody.querySelector(`tr:has(button[data-id="${_migrationConnId || ''}"])`);
    tbody.insertAdjacentHTML(target ? 'afterend' : 'beforeend', _migrationRowHtml(_migrationState));
}

// --- BYO move-out (IMPL-BYO-MOVE-OUT-C-DASHBOARD, design s4) --------------
// One card, five states, CE wording copied from
// results-1110/DESIGN-BYO-MOVE-OUT-FLOW.md section 4. The dialog keys on the
// structured 409 (err.detail.error === 'byo_storage_not_empty'), never on the
// message text. No quantity caps, no licence wording; soft delete only —
// nothing here deletes anything.
const MOVE_OUT_BLOCKED_MSG =
    'Your own storage still has files in it. MOD can\'t switch you back to MOD storage while those files are there — they\'d stop showing up. Move them into MOD storage first. Nothing is deleted from your storage until you confirm.';
const MOVE_OUT_MOVING_MSG =
    'This can take a few minutes. Your files keep working while they move — nothing is deleted from your storage.';
const MOVE_OUT_FOREIGN_MSG =
    'Your bucket still holds files MOD never used. MOD leaves them there; they stay yours.';
const MOVE_OUT_FAILED_MSG =
    'Nothing was deleted and MOD did not change which storage you use.';

let _movePoller = null;
let _moveConnId = null;
let _moveState = null;
let _moveTargetOpts = null;
// Connection id -> name, refreshed by renderConnections; used only to name
// the destination on the moving line. Null target = unknown (e.g. page reload).
let _connNames = {};

function _moveDestLabel() {
    if (!_moveTargetOpts) return null;
    const id = _moveTargetOpts.target_connection_id;
    if (!id) return 'MOD storage';
    return escapeHtml(_connNames[id] || 'another storage');
}

function startMoveProgress(connId) {
    stopMoveProgress();
    _moveConnId = connId;
    _moveState = { move_status: 'pending', moved_object_count: 0, moved_bytes: 0, total_objects: null };
    renderMoveRow();
    _movePoller = new BackoffPoller(() => pollMoveStatus(connId), { baseMs: 3000, maxMs: 30000 });
    _movePoller.start();
}

function stopMoveProgress() {
    if (_movePoller) { _movePoller.stop(); _movePoller = null; }
}

async function pollMoveStatus(connId) {
    if (_moveConnId !== connId) return;
    const st = await storageConnections.moveStatus(connId);
    _moveState = st;
    if (st.move_status === 'verified') {
        stopMoveProgress();
        _moveState = st;
        await loadConnections();
        renderMoveRow();
    } else if (st.move_status === 'failed' || st.move_status === 'cancelled') {
        stopMoveProgress();
        _moveState = st;
        await loadConnections();
        renderMoveRow();
    } else {
        renderMoveRow();
    }
}

function _moveRowHtml(st) {
    const connId = _moveConnId || '';
    const total = (typeof st.total_objects === 'number') ? st.total_objects : null;
    const done = st.moved_object_count || 0;
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : null;
    const startedAt = st.started_at ? Date.now() - new Date(st.started_at).getTime() : null;
    const bar = pct != null
        ? `<span class="d-inline-block bg-blue-500 h-1" style="width:${pct}%"></span>`
        : '';
    const keys = Array.isArray(st.missing_source_keys) ? st.missing_source_keys : [];
    const missing = (typeof st.missing_source_count === 'number') ? st.missing_source_count : keys.length;
    const keyList = keys.length
        ? `<ul class="list-disc ml-5 mt-1 text-xs text-gray-700" id="storage-move-skipped-keys">${keys.map((k) => `<li><code>${escapeHtml(k)}</code></li>`).join('')}</ul>`
        : '';
    const more = missing > keys.length ? `<div class="text-xs text-gray-500">Showing the first ${keys.length} of ${missing}.</div>` : '';
    let lead, note, actions = '';
    if (st.move_status === 'verified' && st.skipped_missing === true) {
        lead = `<span class="text-green-600 font-medium">Move finished — some files were already gone</span>`;
        note = `Migration finished — ${done} of ${total != null ? total : done + missing} objects copied, ${missing} ${missing === 1 ? 'was' : 'were'} already missing from the old backend (listed below). ${missing === 1 ? 'That file is' : 'Those files are'} no longer readable; the rest of your library is live.${keyList}${more}`;
        actions = `<button data-act="deactivate" data-id="${escapeHtml(connId)}" class="text-amber-600 hover:underline mr-3">Switch back to MOD storage</button>`;
    } else if (st.move_status === 'verified') {
        lead = `<span class="text-green-600 font-medium">Move complete</span>`;
        note = `All ${done} files are now in MOD storage. Your storage is empty of MOD's files, so MOD can switch back.`;
        actions = `<button data-act="deactivate" data-id="${escapeHtml(connId)}" class="text-amber-600 hover:underline mr-3">Switch back to MOD storage</button>`;
    } else if (st.move_status === 'failed') {
        lead = `<span class="text-red-600 font-medium">Move failed</span>`;
        note = `${total != null ? `The move stopped at file ${done + 1} of ${total}. ` : ''}${MOVE_OUT_FAILED_MSG} <span class="text-red-600">${escapeHtml(st.move_error || 'unknown error')}</span>${keyList}`;
        actions = `<button data-act="move-retry" data-id="${escapeHtml(connId)}" class="text-primary hover:underline mr-3">Try again</button>`;
    } else if (st.move_status === 'cancelled') {
        lead = `<span class="text-gray-600 font-medium">Move cancelled</span>`;
        note = `The move was stopped. Nothing was deleted and MOD did not change which storage you use.`;
    } else {
        lead = `<span class="text-blue-600 font-medium">${st.move_status === 'running' ? 'Moving' : 'Waiting for worker'}…</span>`;
        const dest = _moveDestLabel();
        const into = dest ? ` into ${dest}` : '';
        note = `${total != null ? `Moving ${done} of ${total} files${into}. ` : `Moving ${done} files${into}. `}${MOVE_OUT_MOVING_MSG} ${pct != null ? `(${pct}%)` : ''} · ${_fmtBytes(st.moved_bytes || 0)} · ${_fmtElapsed(startedAt)} ${bar}`;
        actions = `<button data-act="move-cancel" data-id="${escapeHtml(connId)}" class="text-red-600 hover:underline mr-3">Cancel</button>`;
    }
    return `
        <tr id="storage-move-progress-row" class="border-b bg-blue-50/60">
            <td colspan="6" class="py-2 px-4 text-sm">
                ${lead} &nbsp; <span class="text-gray-600">${note}</span>
                <div class="mt-2 flex justify-end gap-2">${actions}</div>
            </td>
        </tr>`;
}

function renderMoveRow() {
    if (!_moveState) return;
    const tbody = document.querySelector('#storage-connections-table tbody');
    if (!tbody) return;
    const existing = document.getElementById('storage-move-progress-row');
    if (existing) existing.remove();
    const target = tbody.querySelector(`tr:has(button[data-id="${_moveConnId || ''}"])`);
    tbody.insertAdjacentHTML(target ? 'afterend' : 'beforeend', _moveRowHtml(_moveState));
    // The progress row is inserted AFTER renderConnections wired the table
    // buttons, so its own action buttons get their listeners here.
    const row = document.getElementById('storage-move-progress-row');
    if (row) row.querySelectorAll('button[data-act]').forEach(btn => {
        btn.addEventListener('click', () => handleRowAction(btn.dataset.act, parseInt(btn.dataset.id, 10)));
    });
}

async function startMoveOut(connId, targetOpts) {
    try {
        const res = await storageConnections.moveOut(connId, targetOpts);
        _moveTargetOpts = targetOpts;
        await loadConnections();
        if (res && (res.status === 'pending' || res.status === 'running')) startMoveProgress(connId);
    } catch (err) {
        console.error('storage-connection move-out failed:', err);
        alert(`Action failed: ${(err && err.message) || err}`);
    }
}

// The blocked card (state 1). Resolves {targetOpts} to start a move,
// {selfDeleted:true} to re-fire the deactivate (the server re-checks — a claim
// is never trusted and nothing here deletes anything), or null on Cancel.
async function confirmMoveOutTarget(connId, candidates) {
    return new Promise((resolve) => {
        const overlay = document.createElement('div');
        overlay.id = 'move-out-storage-confirm';
        overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        const targetButtons = candidates.map((c) => `
            <button type="button" data-move-target="${c.id}" class="px-3 py-1 border rounded text-sm">Move into "${escapeHtml(c.name)}"</button>`).join('');
        overlay.innerHTML = `
            <div class="bg-white rounded-lg shadow-xl p-5 w-full max-w-md">
                <h3 class="text-lg font-semibold mb-2">Move the files out first?</h3>
                <p class="text-sm text-gray-600 mb-3">${escapeHtml(MOVE_OUT_BLOCKED_MSG)}</p>
                <div class="flex flex-col gap-2">
                    <button type="button" id="move-out-mod" class="px-3 py-1 bg-green-600 text-white rounded text-sm">Move into MOD storage</button>
                    ${targetButtons}
                    <button type="button" id="move-out-self" class="px-3 py-1 border rounded text-sm">I deleted them myself</button>
                    <button type="button" id="move-out-cancel" class="px-3 py-1 border rounded text-sm">Cancel</button>
                </div>
            </div>`;
        const done = (val) => {
            document.removeEventListener('keydown', onKey);
            overlay.remove();
            resolve(val);
        };
        const onKey = (e) => { if (e.key === 'Escape') done(null); };
        document.addEventListener('keydown', onKey);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) done(null); });
        overlay.querySelector('#move-out-cancel').addEventListener('click', () => done(null));
        overlay.querySelector('#move-out-mod').addEventListener('click', () => done({ targetOpts: { target: 'mod_storage' } }));
        overlay.querySelector('#move-out-self').addEventListener('click', () => done({ selfDeleted: true }));
        overlay.querySelectorAll('button[data-move-target]').forEach(btn => {
            btn.addEventListener('click', () => done({ targetOpts: { target_connection_id: parseInt(btn.dataset.moveTarget, 10) } }));
        });
        document.body.appendChild(overlay);
        overlay.querySelector('#move-out-cancel').focus();
    });
}

async function openMoveOutDialog(connId) {
    let candidates = [];
    try {
        const all = await storageConnections.list();
        // Only the tenant's own ACTIVE connections that already passed a test
        // can take the files (the server re-checks both rules).
        candidates = (all || []).filter((c) => c && c.is_active && c.last_test_ok && c.id !== connId);
    } catch (e) { candidates = []; }
    const choice = await confirmMoveOutTarget(connId, candidates);
    if (!choice) return;
    if (choice.selfDeleted === true) {
        try {
            await storageConnections.deactivate(connId);
            await loadConnections();
        } catch (err) {
            if (err && err.detail && err.detail.error === 'byo_storage_not_empty') {
                alert('Your storage still answers that it holds files. Move them with MOD first, or deactivate again once you are sure they are gone.');
            } else {
                alert(`Action failed: ${(err && err.message) || err}`);
            }
        }
        return;
    }
    await startMoveOut(connId, choice.targetOpts);
}

export async function initializeStorageConnectionsPage() {
    await loadConnections();
    await loadStorageUsageSection();

    const addBtn = document.getElementById('add-storage-connection-button');
    if (addBtn) addBtn.addEventListener('click', () => openConnectionModal(null));

    const corsBtn = document.getElementById('storage-cors-help');
    if (corsBtn) corsBtn.addEventListener('click', showCorsRequirements);
}

// megaplan 25.3 Q.5b — Usage section: usage bar (used/quota or
// "Unlimited"), 80%/90% alert banner, and three small tables (by type,
// top workflows, recent runs). Every loader hides *-loading and shows
// *-display in a `finally` so a fetch failure never leaves a stuck
// spinner (recurring bug class per today's notes).
function _fmtBytes(bytes) {
    if (typeof bytes !== 'number' || !isFinite(bytes) || bytes < 0) return '—';
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const value = bytes / Math.pow(1024, i);
    const decimals = value >= 100 || i === 0 ? 0 : value >= 10 ? 1 : 2;
    return `${value.toFixed(decimals)} ${units[i]}`;
}

function _rows(tbodyId, items, emptyText) {
    const tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    if (!items || !items.length) {
        tbody.innerHTML = `<tr><td class="text-gray-400 italic py-1">${emptyText}</td></tr>`;
        return;
    }
    tbody.innerHTML = items.map(([label, bytes]) => `
        <tr class="border-b last:border-0">
            <td class="py-1 pr-2 text-gray-700 truncate max-w-[10rem]" title="${escapeHtml(label)}">${escapeHtml(label)}</td>
            <td class="py-1 text-right text-gray-500 tabular-nums">${_fmtBytes(bytes)}</td>
        </tr>`).join('');
}

async function loadStorageUsageSection() {
    const loadingEl = document.getElementById('storage-usage-loading');
    const displayEl = document.getElementById('storage-usage-display');
    if (!loadingEl || !displayEl) return;

    // 2400LR-cdu: /tenant/storage denied on CE (manifest:1327 routers/tenant/*.py);
    // fail-open on unknown edition.
    if (!await isPaidEdition()) {
        loadingEl.classList.add('hidden');
        displayEl.classList.add('hidden');
        return;
    }

    try {
        const data = await tenantStorage.get();
        const totalBytes = data.total_bytes || 0;
        const quotaBytes = data.quota_bytes; // null = unlimited
        const isUnlimited = quotaBytes === null || quotaBytes === undefined;
        const pct = (!isUnlimited && data.percent_used != null) ? data.percent_used : 0;

        const summaryEl = document.getElementById('storage-usage-summary');
        if (summaryEl) {
            summaryEl.textContent = isUnlimited
                ? `${_fmtBytes(totalBytes)} used (Unlimited)`
                : `${_fmtBytes(totalBytes)} of ${_fmtBytes(quotaBytes)} used`;
        }

        const fill = document.getElementById('storage-usage-bar-fill');
        const pctEl = document.getElementById('storage-usage-pct');
        if (fill) {
            fill.style.width = isUnlimited ? '0%' : `${Math.min(100, Math.max(0, pct)).toFixed(1)}%`;
            fill.classList.remove('bg-primary', 'bg-amber-500', 'bg-red-600');
            fill.classList.add(data.alert_level === 'crit_90' ? 'bg-red-600'
                : data.alert_level === 'warn_80' ? 'bg-amber-500' : 'bg-primary');
        }
        if (pctEl) pctEl.textContent = isUnlimited ? 'Unlimited storage' : `${pct.toFixed(1)}% of quota used`;

        const banner = document.getElementById('storage-usage-banner');
        const bannerText = document.getElementById('storage-usage-banner-text');
        if (banner && bannerText) {
            banner.classList.remove('hidden', 'bg-amber-50', 'border-amber-300', 'text-amber-800',
                'bg-red-50', 'border-red-300', 'text-red-800');
            if (data.alert_level === 'crit_90') {
                banner.classList.add('bg-red-50', 'border-red-300', 'text-red-800');
                bannerText.textContent = `Critical: storage is at ${pct.toFixed(1)}% of quota. Free up space to avoid disruption.`;
            } else if (data.alert_level === 'warn_80') {
                banner.classList.add('bg-amber-50', 'border-amber-300', 'text-amber-800');
                bannerText.textContent = `Warning: storage is at ${pct.toFixed(1)}% of quota.`;
            } else {
                banner.classList.add('hidden');
            }
        }

        const byType = data.by_type || {};
        _rows('storage-usage-by-type', Object.entries(byType).map(([k, v]) => [
            k.replace(/_bytes$/, '').replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()), v,
        ]), 'No data yet.');
        _rows('storage-usage-by-workflow', (data.by_workflow || []).map(w => [
            w.workflow_name || `Workflow ${w.workflow_id}`, w.bytes,
        ]), 'No workflows yet.');
        _rows('storage-usage-by-run', (data.by_run || []).map(r => [
            r.started_at ? new Date(r.started_at).toLocaleString() : `Run ${r.run_id}`, r.bytes,
        ]), 'No runs yet.');
    } catch (err) {
        console.error('Error loading storage usage:', err);
        const summaryEl = document.getElementById('storage-usage-summary');
        if (summaryEl) summaryEl.textContent = 'Unable to load usage.';
    } finally {
        loadingEl.classList.add('hidden');
        displayEl.classList.remove('hidden');
    }
}

async function loadConnections() {
    const tbody = document.querySelector('#storage-connections-table tbody');
    if (!tbody) return;
    try {
        const conns = await storageConnections.list();
        renderConnections(conns || []);
    } catch (err) {
        console.error('Error loading storage connections:', err);
        tbody.innerHTML = `<tr><td colspan="6" class="text-center text-red-500 py-4">Could not load storage connections.</td></tr>`;
    }
}

function renderConnections(conns) {
    const tbody = document.querySelector('#storage-connections-table tbody');
    if (!tbody) return;
    _connNames = Object.fromEntries(conns.map(c => [c.id, c.name]));
    if (!conns.length) {
        tbody.innerHTML = `<tr><td colspan="6" class="text-center text-gray-400 py-6">No connections yet. Files use the platform's house-default storage.</td></tr>`;
        return;
    }
    tbody.innerHTML = conns.map(c => {
        const testBadge = c.last_test_ok
            ? `<span class="text-green-600 font-medium">Passed</span>`
            : (c.last_test_error
                ? `<span class="text-red-600" title="${escapeHtml(c.last_test_error)}">Failed</span>`
                : `<span class="text-gray-400">Untested</span>`);
        const statusBadge = c.is_active
            ? `<span class="px-2 py-0.5 rounded-full text-xs bg-green-100 text-green-700">Active</span>`
            : `<span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-500">Inactive</span>`;
        // Deactivate stays live (recovery path back to house default). Activate
        // is hard-gated while the data plane is unfinished (see BYO_ACTIVATION_ENABLED).
        const activateBtn = c.is_active
            ? `<button data-act="deactivate" data-id="${c.id}" class="text-amber-600 hover:underline mr-3">Deactivate</button>`
            : (BYO_ACTIVATION_ENABLED
                ? `<button data-act="activate" data-id="${c.id}" class="text-green-600 hover:underline mr-3 ${c.last_test_ok ? '' : 'opacity-40 cursor-not-allowed'}" ${c.last_test_ok ? '' : 'disabled title="Pass a test first"'}>Activate</button>`
                : `<button class="text-gray-400 mr-3 opacity-40 cursor-not-allowed" disabled title="${escapeHtml(BYO_PREVIEW_MSG)}">Activate (preview)</button>`);
        return `
        <tr class="border-b">
            <td class="py-2 px-4">${escapeHtml(c.name)}</td>
            <td class="py-2 px-4 text-sm text-gray-600">${escapeHtml(c.endpoint)}</td>
            <td class="py-2 px-4 text-sm text-gray-600">${escapeHtml(c.bucket)}</td>
            <td class="py-2 px-4 text-center text-sm">${testBadge}</td>
            <td class="py-2 px-4 text-center">${statusBadge}</td>
            <td class="py-2 px-4 text-right whitespace-nowrap">
                <button data-act="test" data-id="${c.id}" class="text-primary hover:underline mr-3">Test</button>
                ${activateBtn}
                <button data-act="edit" data-id="${c.id}" class="text-gray-600 hover:underline mr-3">Edit</button>
                <button data-act="delete" data-id="${c.id}" class="text-red-600 hover:underline">Remove</button>
            </td>
        </tr>`;
    }).join('');

    tbody.querySelectorAll('button[data-act]').forEach(btn => {
        btn.addEventListener('click', () => handleRowAction(btn.dataset.act, parseInt(btn.dataset.id, 10)));
    });
    // A full re-render wipes the in-place progress row — re-append it when a
    // migration is in flight (or has just reached a terminal state).
    if (_migrationState && _migrationConnId) renderMigrationRow();
    if (_moveState && _moveConnId) renderMoveRow();
}

const SKIP_MISSING_LABEL = 'Skip missing source objects?';
const SKIP_MISSING_HELP = 'Some files may no longer exist in the current storage backend. If you tick this, MOD copies what it can and tells you exactly which objects were already gone. Leave it unticked to stop at the first missing file (default).';

// Small confirm dialog for Activate. Resolves {skipMissingSource: boolean} on
// Activate, or null on Cancel / Escape / backdrop click. The checkbox is
// UNTICKED by default (fail-closed: stop at the first missing file).
function confirmActivate() {
    return new Promise((resolve) => {
        const overlay = document.createElement('div');
        overlay.id = 'activate-storage-confirm';
        overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.innerHTML = `
            <div class="bg-white rounded-lg shadow-xl p-5 w-full max-w-md">
                <h3 class="text-lg font-semibold mb-2">Activate this storage connection?</h3>
                <p class="text-sm text-gray-600 mb-3">Your existing files are copied to this connection and verified before it goes live. Originals stay untouched until verification passes.</p>
                <label class="flex items-start gap-2 text-sm mb-4">
                    <input type="checkbox" id="activate-skip-missing" class="mt-1">
                    <span><strong>${escapeHtml(SKIP_MISSING_LABEL)}</strong><br><span class="text-gray-600">${escapeHtml(SKIP_MISSING_HELP)}</span></span>
                </label>
                <div class="flex justify-end gap-2">
                    <button type="button" id="activate-confirm-cancel" class="px-3 py-1 border rounded">Cancel</button>
                    <button type="button" id="activate-confirm-ok" class="px-3 py-1 bg-green-600 text-white rounded">Activate</button>
                </div>
            </div>`;
        const done = (val) => {
            document.removeEventListener('keydown', onKey);
            overlay.remove();
            resolve(val);
        };
        const onKey = (e) => { if (e.key === 'Escape') done(null); };
        document.addEventListener('keydown', onKey);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) done(null); });
        overlay.querySelector('#activate-confirm-cancel').addEventListener('click', () => done(null));
        overlay.querySelector('#activate-confirm-ok').addEventListener('click', () => {
            done({ skipMissingSource: overlay.querySelector('#activate-skip-missing').checked === true });
        });
        document.body.appendChild(overlay);
        overlay.querySelector('#activate-confirm-cancel').focus();
    });
}

async function handleRowAction(act, id) {
    try {
        if (act === 'test') {
            const res = await storageConnections.test(id);
            alert(res.ok ? `Test passed: ${res.reason}` : `Test failed: ${res.reason}`);
            await loadConnections();
        } else if (act === 'activate') {
            // Defense in depth: the button is disabled while gated, but never
            // fire the activate call from any code path when the gate is off.
            if (!BYO_ACTIVATION_ENABLED) { alert(BYO_PREVIEW_MSG); return; }
            const choice = await confirmActivate();
            if (!choice) return;
            const res = await storageConnections.activate(id, { skipMissingSource: choice.skipMissingSource });
            await loadConnections();
            // WS4.Q17.17 — existing objects: activation enqueued a copy job
            // and the connection is NOT active yet. Track it with a progress
            // row + backoff poller until verified/failed.
            if (res && (res.migration_status === 'pending' || res.migration_status === 'running')) {
                startMigrationProgress(id);
            }
        } else if (act === 'deactivate') {
            stopMigrationProgress();
            stopMoveProgress();
            _migrationConnId = null;
            _migrationState = null;
            _moveConnId = null;
            _moveState = null;
            await storageConnections.deactivate(id);
            await loadConnections();
        } else if (act === 'move-cancel') {
            // Cancel an in-flight move-out (the job row goes to 'cancelled' —
            // history, soft delete only).
            try {
                await storageConnections.moveOutCancel(id);
            } catch (err) {
                if (err && err.detail && err.detail.error === 'move_out_not_running') {
                    alert('No move-out is in flight for this connection.');
                } else {
                    alert(`Action failed: ${(err && err.message) || err}`);
                }
            }
            stopMoveProgress();
            _moveConnId = null;
            _moveState = null;
            await loadConnections();
        } else if (act === 'move-retry') {
            // "Try again" re-arms the move with the target the user picked.
            if (!_moveTargetOpts) { await openMoveOutDialog(id); return; }
            await startMoveOut(id, _moveTargetOpts);
        } else if (act === 'delete') {
            if (!confirm('Remove this storage connection? Files will fall back to the house default.')) return;
            await storageConnections.remove(id);
            await loadConnections();
        } else if (act === 'edit') {
            const conn = await storageConnections.get(id);
            openConnectionModal(conn);
        }
    } catch (err) {
        console.error(`storage-connection ${act} failed:`, err);
        // BYO-DEACTIVATE-BLOCK + move-out (design s4): the 409 is keyed on the
        // STRUCTURED detail, never the message text — open the move-out card.
        if (act === 'deactivate' && err && err.detail && err.detail.error === 'byo_storage_not_empty') {
            await openMoveOutDialog(id);
            return;
        }
        alert(`Action failed: ${(err && err.message) || err}`);
    }
}

async function openConnectionModal(conn) {
    const modal = await loadModal('add-storage-connection');
    if (!modal) return;
    const isEdit = !!conn;

    const $ = (id) => document.getElementById(id);
    $('storage-modal-title').textContent = isEdit ? 'Edit Storage Connection' : 'Add Storage Connection';
    if (isEdit) {
        $('storage-conn-id').value = conn.id;
        $('storage-conn-name').value = conn.name || '';
        $('storage-conn-endpoint').value = conn.endpoint || '';
        $('storage-conn-region').value = conn.region || 'us-east-1';
        $('storage-conn-bucket').value = conn.bucket || '';
        $('storage-conn-access-key').value = conn.access_key || '';
        $('storage-conn-secure').checked = conn.secure !== false;
        // Secret is never returned; make it optional on edit.
        $('storage-conn-secret-key').required = false;
        $('storage-conn-secret-hint').classList.remove('hidden');
    }

    $('cancel-storage-conn-button').addEventListener('click', () => closeModal('add-storage-connection-modal'));
    $('storage-conn-test-button').addEventListener('click', () => testFromModal());
    $('add-storage-connection-form').addEventListener('submit', (e) => submitConnection(e, isEdit));
}

function readModalFields() {
    const $ = (id) => document.getElementById(id);
    return {
        id: $('storage-conn-id').value ? parseInt($('storage-conn-id').value, 10) : null,
        name: $('storage-conn-name').value.trim(),
        endpoint: $('storage-conn-endpoint').value.trim(),
        region: $('storage-conn-region').value.trim() || 'us-east-1',
        bucket: $('storage-conn-bucket').value.trim(),
        access_key: $('storage-conn-access-key').value.trim(),
        secret_key: $('storage-conn-secret-key').value,
        secure: $('storage-conn-secure').checked,
    };
}

function showTestResult(ok, reason, cors) {
    const el = document.getElementById('storage-conn-test-result');
    if (!el) return;
    el.classList.remove('hidden', 'bg-green-50', 'text-green-700', 'bg-red-50', 'text-red-700');
    if (ok) {
        el.classList.add('bg-green-50', 'text-green-700');
        el.textContent = `✓ ${reason}`;
    } else {
        el.classList.add('bg-red-50', 'text-red-700');
        el.textContent = `✗ ${reason}${cors ? '  (' + cors + ')' : ''}`;
    }
}

async function testFromModal() {
    const f = readModalFields();
    if (!f.secret_key) {
        // Editing without a new secret: test the saved connection using its stored secret.
        if (f.id) {
            try {
                const res = await storageConnections.test(f.id);
                showTestResult(res.ok, res.reason, res.cors_note);
            } catch (err) {
                showTestResult(false, (err && err.message) || String(err));
            }
            return;
        }
        showTestResult(false, 'Enter a secret key to test.');
        return;
    }
    try {
        const res = await storageConnections.testConfig({
            endpoint: f.endpoint, region: f.region, bucket: f.bucket,
            access_key: f.access_key, secret_key: f.secret_key, secure: f.secure,
        });
        showTestResult(res.ok, res.reason, res.cors_note);
    } catch (err) {
        showTestResult(false, (err && err.message) || String(err));
    }
}

async function submitConnection(e, isEdit) {
    e.preventDefault();
    const f = readModalFields();
    try {
        if (isEdit) {
            const payload = {
                name: f.name, endpoint: f.endpoint, region: f.region,
                bucket: f.bucket, access_key: f.access_key, secure: f.secure,
            };
            if (f.secret_key) payload.secret_key = f.secret_key;  // only if changed
            await storageConnections.update(f.id, payload);
        } else {
            await storageConnections.create({
                name: f.name, endpoint: f.endpoint, region: f.region,
                bucket: f.bucket, access_key: f.access_key,
                secret_key: f.secret_key, secure: f.secure,
            });
        }
        closeModal('add-storage-connection-modal');
        await loadConnections();
    } catch (err) {
        console.error('Save storage connection failed:', err);
        showTestResult(false, `Save failed: ${(err && err.message) || err}`);
    }
}

async function showCorsRequirements() {
    const panel = document.getElementById('storage-cors-panel');
    const pre = document.getElementById('storage-cors-json');
    const note = document.getElementById('storage-cors-note');
    if (!panel) return;
    try {
        const req = await storageConnections.corsRequirements();
        pre.textContent = JSON.stringify(req.example_s3_cors_json, null, 2);
        note.textContent = `${req.note}  ${req.versitygw_house_default_hint || ''}`;
        panel.classList.toggle('hidden');
    } catch (err) {
        console.error('CORS requirements fetch failed:', err);
    }
}

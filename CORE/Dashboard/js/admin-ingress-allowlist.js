// Dashboard/js/admin-ingress-allowlist.js
//
// FX.145.p3c — "Admin network allowlist" card within the Blocked IPs tab
// (Security hub). Controls the platform settings:
//   security.admin_ingress_cidrs  list[str]  — one CIDR per line
//   security.admin_ingress_paths  list[str]  — read-only display
//
// Uses the same PATCH /platform/settings/{key} route as the generic
// platform-settings.js editor (api.js platformSettings.patch).
// Server returns 422 on validation failure — surfaced inline in #ai-error.
// Before saving a non-empty list, the user sees a confirmation with the
// lockout warning. An empty list is allowed without confirm (clears the list).
// No innerHTML with server data — textContent only. Tailwind: no blue brand colours.

import { platformSettings, me } from './api.js';

// ---------------------------------------------------------------------------
// Escaping (textContent for all server data)
// ---------------------------------------------------------------------------

function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _errText(e) {
    if (!e) return '';
    const detail = e.detail !== undefined ? e.detail : e.message;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
        return detail.map(d => `${d.loc || d.field || ''}: ${d.msg || d.detail || ''}`)
            .join('; ');
    }
    return String(detail);
}

function _showError(msg) {
    const box = document.getElementById('ai-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('ai-error');
    if (box) box.classList.add('hidden');
}

// ---------------------------------------------------------------------------
// Current value display
// ---------------------------------------------------------------------------

let _currentCidrs = [];
let _currentPaths = [];

function renderCurrentValues() {
    const cidrBox = document.getElementById('ai-cidrs-value');
    if (cidrBox) {
        cidrBox.textContent = _currentCidrs.length
            ? _currentCidrs.join('\n')
            : '(empty — any network may reach admin pages)';
    }
    const pathsBox = document.getElementById('ai-paths-value');
    if (pathsBox) {
        pathsBox.textContent = _currentPaths.length
            ? _currentPaths.join(', ')
            : '(empty — default paths apply)';
    }
}

// ---------------------------------------------------------------------------
// Parse / serialize CIDR textarea (one per line)
// ---------------------------------------------------------------------------

function getCidrsFromTextarea() {
    const el = document.getElementById('ai-cidrs-input');
    if (!el) return [];
    return el.value
        .split('\n')
        .map(s => s.trim())
        .filter(s => s.length > 0);
}

// ---------------------------------------------------------------------------
// Load current settings
// ---------------------------------------------------------------------------

async function loadValues() {
    try {
        const [cidrsRes, pathsRes] = await Promise.all([
            platformSettings.patch('security.admin_ingress_cidrs', null),
            platformSettings.patch('security.admin_ingress_paths', null),
        ]);
        _currentCidrs = Array.isArray(cidrsRes && cidrsRes.value) ? cidrsRes.value : [];
        _currentPaths = Array.isArray(pathsRes && pathsRes.value) ? pathsRes.value : [];
    } catch (e) {
        // If the settings API is not yet available, show a gentle notice and
        // let the user proceed anyway (the save will fail gracefully).
        const note = document.getElementById('ai-load-note');
        if (note) {
            note.textContent = 'Platform settings API not available — values cannot be loaded or saved.';
            note.classList.remove('hidden');
        }
        _currentCidrs = [];
        _currentPaths = [];
    }
    renderCurrentValues();
}

// ---------------------------------------------------------------------------
// Save
// ---------------------------------------------------------------------------

async function handleSave() {
    _clearError();

    const cidrs = getCidrsFromTextarea();

    // Non-empty list: confirm with the lockout warning.
    if (cidrs.length > 0) {
        const confirmed = confirm(
            'Only these networks will reach admin pages. Include the network you are on now, or you will lock yourself out.'
        );
        if (!confirmed) return;
    }

    const saveBtn = document.getElementById('ai-save-btn');
    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = 'Saving…';
    }

    try {
        const res = await platformSettings.patch('security.admin_ingress_cidrs', cidrs);
        _currentCidrs = Array.isArray(res.value) ? res.value : cidrs;
        renderCurrentValues();
        // Success toast (if the global helper exists).
        if (window.showInfo) {
            window.showInfo(
                cidrs.length > 0
                    ? `Admin ingress now restricted to ${cidrs.length} network(s).`
                    : 'Admin ingress allowlist cleared — all networks may reach admin pages again.',
                'Admin network allowlist'
            );
        }
    } catch (e) {
        // 422 = validation error from the server (bad CIDR, wrong type, etc.)
        _showError(_errText(e) || `Save failed (status ${e && e.status || '?'})`);
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = 'Save';
        }
    }
}

// ---------------------------------------------------------------------------
// Init (called when the Blocked IPs tab panel loads)
// ---------------------------------------------------------------------------

export async function initializeAdminIngressAllowlist() {
    const saveBtn = document.getElementById('ai-save-btn');
    if (saveBtn && !saveBtn.dataset.wired) {
        saveBtn.dataset.wired = '1';
        saveBtn.addEventListener('click', handleSave);
    }
    await loadValues();
}

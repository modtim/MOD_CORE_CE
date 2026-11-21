// Outbound webhooks — create / list / delete WebhookEndpoint rows for the
// current user's tenant. Backed by the /webhooks/ router; the API enforces
// tenant scoping (rows are always created + listed for the caller's tenant).
//
// BUG.B36b (owner Q-B36=A): the tenant-facing UI now lives inside System → Apps
// → Webhooks (pages/apps.html + js/apps.js), which reuses the helpers exported
// below. initializeWebhooksPage() is kept for the legacy #webhooks hash route
// (Settings → Integrations signpost, BUG.B36c) so old bookmarks still resolve.
import { webhooks } from './api.js';

function _esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _msg(el, text, ok) {
    if (!el) return;
    el.textContent = text || '';
    el.className = 'text-sm ' + (ok ? 'text-emerald-600' : 'text-destructive');
}

function _renderRows(list) {
    const tbody = document.getElementById('wh-rows');
    if (!tbody) return;
    if (!Array.isArray(list) || list.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="px-5 py-6 text-center text-gray-400">No webhook endpoints yet.</td></tr>';
        return;
    }
    tbody.innerHTML = list.map(w => {
        const events = (w.subscribed_events || []).map(e => `<code class="bg-gray-100 px-1 rounded text-xs">${_esc(e)}</code>`).join(' ') || '<span class="text-gray-400">none</span>';
        const state = w.is_active
            ? '<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-100 text-emerald-700">active</span>'
            : '<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600">inactive</span>';
        const desc = w.description ? `<div class="text-xs text-gray-400 mt-0.5">${_esc(w.description)}</div>` : '';
        return `<tr class="border-t border-gray-100 align-top">
            <td class="px-5 py-3 text-gray-500">${_esc(w.id)}</td>
            <td class="px-5 py-3 text-gray-900 break-all max-w-xs">${_esc(w.url)}${desc}</td>
            <td class="px-5 py-3">${events}</td>
            <td class="px-5 py-3">${state}</td>
            <td class="px-5 py-3 text-right">
                <button data-wh-del="${_esc(w.id)}" class="px-3 py-1.5 text-xs font-medium text-red-600 bg-red-50 rounded-lg hover:bg-red-100">Delete</button>
            </td>
        </tr>`;
    }).join('');
    tbody.querySelectorAll('[data-wh-del]').forEach(btn => {
        btn.addEventListener('click', () => _deleteEndpoint(btn.getAttribute('data-wh-del')));
    });
}

// BUG.B36b — exported so js/apps.js (the merged System → Apps → Webhooks list)
// can reuse the tenant-wide "All workflows" backend without duplicating paths.
export async function listEndpoints() {
    return await webhooks.list();
}

export async function createEndpoint(data) {
    return await webhooks.create(data);
}

export async function removeEndpoint(id) {
    return await webhooks.remove(id);
}

async function _load() {
    const tbody = document.getElementById('wh-rows');
    try {
        const list = await listEndpoints();
        _renderRows(list);
    } catch (e) {
        if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="px-5 py-6 text-center text-destructive">Could not load endpoints: ${_esc(e.message || String(e))}</td></tr>`;
    }
}

async function _create() {
    const msgEl = document.getElementById('wh-create-msg');
    const url = (document.getElementById('wh-url')?.value || '').trim();
    const description = (document.getElementById('wh-desc')?.value || '').trim();
    const is_active = !!document.getElementById('wh-active')?.checked;
    const subscribed_events = Array.from(document.querySelectorAll('.wh-event'))
        .filter(cb => cb.checked).map(cb => cb.value);

    if (!url) { _msg(msgEl, 'A destination URL is required.', false); return; }
    if (subscribed_events.length === 0) { _msg(msgEl, 'Select at least one event.', false); return; }

    const btn = document.getElementById('wh-create-btn');
    if (btn) btn.disabled = true;
    _msg(msgEl, 'Creating…', true);
    try {
        const created = await createEndpoint({ url, description: description || null, subscribed_events, is_active });
        _msg(msgEl, `Created endpoint #${created.id}.`, true);
        // reset form
        const urlEl = document.getElementById('wh-url'); if (urlEl) urlEl.value = '';
        const descEl = document.getElementById('wh-desc'); if (descEl) descEl.value = '';
        await _load();
    } catch (e) {
        // Real error surfaced from the API — never a false success.
        _msg(msgEl, 'Create failed: ' + (e.message || String(e)), false);
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function _deleteEndpoint(id) {
    if (!window.confirm(`Delete webhook endpoint #${id}?`)) return;
    const msgEl = document.getElementById('wh-create-msg');
    try {
        await removeEndpoint(id);
        _msg(msgEl, `Deleted endpoint #${id}.`, true);
        await _load();
    } catch (e) {
        _msg(msgEl, 'Delete failed: ' + (e.message || String(e)), false);
    }
}

export function initializeWebhooksPage() {
    document.getElementById('wh-create-btn')?.addEventListener('click', _create);
    document.getElementById('wh-refresh-btn')?.addEventListener('click', _load);
    _load();
}

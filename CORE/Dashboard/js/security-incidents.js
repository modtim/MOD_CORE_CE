// GSEC FX.50 p3a — Security-incident tracker page (platform admin only).
//
// Backend: routers/platform_security_incidents.py, mounted at /v1 (see
// CORE/API/main.py include_router(..., prefix="/v1")):
//   GET  /platform/security-incidents?status=&limit=&offset= -> {items,total,limit,offset}
//   GET  /platform/security-incidents/{id}
//   POST /platform/security-incidents            {tenant_id,severity,summary,detected_at}
//   POST /platform/security-incidents/{id}/confirm            {confirmed_at?}
//   GET  /platform/security-incidents/{id}/follow-ups         -> {items,total}
//   POST /platform/security-incidents/{id}/follow-ups         {owner,due_at,note?}
//   POST /platform/security-incidents/{id}/follow-ups/{fu}/complete
//   POST /platform/security-incidents/{id}/notify
//   POST /platform/security-incidents/{id}/close              {note?}
// DTO fields rendered (SecurityIncidentOut): id, tenant_id, severity, summary,
// status, detected_at, confirmed_at, notify_due_at, gov_headsup_due_at,
// notified_at, closed_at.
// The OVERDUE flag is computed here — the DTO carries none. services.overdue()
// is a separate endpoint; the page derives the same verdict client-side: an
// incident that is not yet notified/closed whose notify_due_at (72h, Q33=A)
// or gov_headsup_due_at (24h, gov/DoD installs only) clock has already passed.
// Email delivery + the in-app banner are FX.50 p3b — this page only records
// the notify moment (mark_notified clears the deadline clocks).
import { securityIncidents, me } from './api.js';
import { withButtonBusy } from './utils.js';

let _statusFilter = null;    // open | confirmed | notified | closed (API `status`)
let _overdueOnly = false;    // client-side filter (the API has no overdue_only here)
let _selectedId = null;      // incident whose follow-up panel is shown

function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _fmt(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 10);
}

function _showError(msg) {
    const box = document.getElementById('si-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('si-error');
    if (box) box.classList.add('hidden');
}

function _errText(e) {
    // Verbatim server detail (string as-is; FastAPI 422 arrays joined as
    // "field: msg" pairs — same shape as account.js _formatScError).
    if (!e) return '';
    const detail = e.detail !== undefined ? e.detail : e.message;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
        return detail.map(x => {
            if (!x || typeof x !== 'object') return String(x);
            const loc = Array.isArray(x.loc) ? x.loc.filter(p => p !== 'body').join('.') : '';
            return loc ? `${loc}: ${x.msg || 'invalid'}` : (x.msg || JSON.stringify(x));
        }).join('; ');
    }
    if (detail && typeof detail === 'object') {
        try { return JSON.stringify(detail); } catch { return 'Unknown error'; }
    }
    return (e && e.message) || 'Unknown error';
}

// Deadline verdict for one incident row (mirrors services.overdue() reasons).
function _dueInfo(row) {
    if (row.status === 'closed' || row.notified_at) {
        return { label: row.notified_at ? `notified ${_fmt(row.notified_at)}` : 'closed',
                 cls: 'bg-gray-100 text-gray-500', overdue: false };
    }
    if (!row.notify_due_at && !row.gov_headsup_due_at) {
        return { label: 'no clocks yet', cls: 'bg-gray-100 text-gray-500', overdue: false };
    }
    const reasons = [];
    let worst = null;
    [['gov_headsup_due_at', 'gov heads-up'], ['notify_due_at', 'notification']]
        .forEach(([key, name]) => {
            const raw = row[key];
            if (!raw) return;
            const due = new Date(raw);
            if (isNaN(due.getTime())) return;
            const days = Math.ceil((due.getTime() - Date.now()) / 86400000);
            if (days < 0) reasons.push(`${name} ${-days}d late`);
            if (worst === null || days < worst) worst = days;
        });
    if (reasons.length) {
        return { label: `OVERDUE ${reasons.join(' · ')}`, cls: 'bg-red-100 text-red-700',
                 overdue: true };
    }
    if (worst === null) return { label: 'Date unreadable', cls: 'bg-amber-100 text-amber-700', overdue: false };
    if (worst === 0) return { label: 'Due today', cls: 'bg-amber-100 text-amber-700', overdue: false };
    return { label: `Due in ${worst}d`, cls: 'bg-green-100 text-green-700', overdue: false };
}

// ---------------------------------------------------------------------------
// init + controls
// ---------------------------------------------------------------------------

export async function initializeSecurityIncidentsPage() {
    ['si-filter-open', 'si-filter-confirmed', 'si-filter-notified', 'si-filter-closed'].forEach(id => {
        const btn = document.getElementById(id);
        if (!btn || btn.dataset.wired) return;
        btn.dataset.wired = '1';
        const status = id.replace('si-filter-', '');
        btn.addEventListener('click', () => {
            _statusFilter = (_statusFilter === status) ? null : status;
            btn.classList.toggle('bg-slate-200');
            loadTable();
        });
    });
    const overdueToggle = document.getElementById('si-overdue-toggle');
    if (overdueToggle && !overdueToggle.dataset.wired) {
        overdueToggle.dataset.wired = '1';
        overdueToggle.addEventListener('click', () => {
            _overdueOnly = !_overdueOnly;
            overdueToggle.classList.toggle('bg-red-100');
            loadTable();
        });
    }
    const newBtn = document.getElementById('si-new-btn');
    if (newBtn && !newBtn.dataset.wired) {
        newBtn.dataset.wired = '1';
        newBtn.addEventListener('click', () => { handleCreate(); });
    }
    const fuBtn = document.getElementById('si-fu-add-btn');
    if (fuBtn && !fuBtn.dataset.wired) {
        fuBtn.dataset.wired = '1';
        fuBtn.addEventListener('click', () => { handleFollowUpAdd(); });
    }
    await loadTable();
}

async function loadTable() {
    _clearError();
    const tbody = document.getElementById('si-table-body');
    const empty = document.getElementById('si-empty');
    if (!tbody) return;
    if (empty) empty.classList.add('hidden');
    tbody.innerHTML = '<tr><td colspan="9" class="px-4 py-6 text-gray-500">Loading…</td></tr>';
    try {
        const [list, user] = await Promise.all([
            securityIncidents.list({
                status: _statusFilter,
                limit: 500,
            }),
            me.get(),
        ]);
        if (user && user.is_platform_admin === false) {
            // The nav hides the page for non-admins; a deep link still must
            // not render the tracker (the API 403s anyway — belt and braces).
            tbody.innerHTML = '';
            _showError('Security incidents are platform-admin only.');
            return;
        }
        let items = (list && list.items) || [];
        items = items.map(row => {
            const due = _dueInfo(row);
            return { ...row, _overdue: due.overdue, _due: due };
        });
        if (_overdueOnly) items = items.filter(r => r._overdue);
        const count = document.getElementById('si-count');
        if (count) {
            const total = (list && list.total != null) ? list.total : items.length;
            const overdueN = items.filter(r => r._overdue).length;
            const bits = [`${total} incident(s)`];
            if (_statusFilter) bits.push(`status ${_statusFilter}`);
            if (_overdueOnly) bits.push('overdue only');
            if (overdueN) bits.push(`${overdueN} overdue`);
            count.textContent = bits.join(' · ');
        }
        if (!items.length) {
            tbody.innerHTML = '';
            if (empty) empty.classList.remove('hidden');
            return;
        }
        tbody.innerHTML = items.map(rowHtml).join('');
        tbody.querySelectorAll('[data-si-action]').forEach(btn => {
            btn.addEventListener('click', () =>
                handleAction(btn.dataset.siAction, btn.dataset.siId));
        });
    } catch (e) {
        tbody.innerHTML = '';
        _showError(_errText(e) || 'Failed to load the security-incident tracker');
    }
}

function rowHtml(r) {
    const sevCls = r.severity === 'critical'
        ? 'bg-red-100 text-red-700'
        : (r.severity === 'high' ? 'bg-orange-100 text-orange-700'
            : (r.severity === 'medium' ? 'bg-amber-100 text-amber-700' : 'bg-gray-100 text-gray-600'));
    const statusCls = r.status === 'open'
        ? 'bg-amber-100 text-amber-800'
        : (r.status === 'confirmed' ? 'bg-blue-100 text-blue-700'
            : (r.status === 'notified' ? 'bg-green-100 text-green-700' : 'bg-gray-200 text-gray-600'));
    const due = r._due || _dueInfo(r);
    const open = r.status !== 'closed';
    const actions = !open
        ? `<span class="text-xs text-gray-400">closed ${_fmt(r.closed_at)}</span>`
        : [
            (r.status === 'open'
                ? `<button type="button" data-si-action="confirm" data-si-id="${r.id}" class="text-blue-700 hover:underline text-sm">Confirm</button>`
                : `<span class="text-xs text-gray-400">confirmed ${_fmt(r.confirmed_at)}</span>`),
            `<button type="button" data-si-action="notify" data-si-id="${r.id}" class="text-green-700 hover:underline text-sm">Mark notified</button>`,
            `<button type="button" data-si-action="close" data-si-id="${r.id}" class="text-gray-600 hover:underline text-sm">Close</button>`,
        ].join(' ');
    return `<tr class="hover:bg-gray-50">
        <td class="px-4 py-3 font-mono text-xs">${esc(r.id)}</td>
        <td class="px-4 py-3 whitespace-nowrap"><span class="px-2 py-0.5 rounded-full text-xs ${sevCls}">${esc(r.severity || '—')}</span></td>
        <td class="px-4 py-3 whitespace-nowrap"><span class="px-2 py-0.5 rounded-full text-xs ${statusCls}">${esc(r.status || '—')}</span></td>
        <td class="px-4 py-3 whitespace-nowrap">${esc(r.summary || '—')}</td>
        <td class="px-4 py-3 whitespace-nowrap">${esc(r.tenant_id == null ? 'platform-wide' : `tenant #${r.tenant_id}`)}</td>
        <td class="px-4 py-3 whitespace-nowrap font-mono text-xs">${_fmt(r.detected_at)}</td>
        <td class="px-4 py-3 whitespace-nowrap font-mono text-xs">${_fmt(r.confirmed_at)}</td>
        <td class="px-4 py-3 whitespace-nowrap"><span class="px-2 py-0.5 rounded-full text-xs ${due.cls}">${esc(due.label)}</span></td>
        <td class="px-4 py-3 text-right whitespace-nowrap">${actions}</td>
    </tr>`;
}

// ---------------------------------------------------------------------------
// Row actions — confirm / notify / close. Each is behind confirm(); the
// confirm copy spells out the deadline clocks the server stamps (72h
// notification, 24h gov heads-up on gov/DoD installs only).
// ---------------------------------------------------------------------------

async function handleAction(action, id) {
    _clearError();
    const labels = {
        confirm: `Confirm security incident ${id}? This starts the deadline clocks: notify_due_at = +72h, and on gov/DoD installs gov_headsup_due_at = +24h.`,
        notify: `Mark security incident ${id} as notified (the affected tenant was told)? This clears the deadline clocks. The breach email itself is a later step.`,
        close: `Close security incident ${id}? It moves to 'closed' and stops being actionable.`,
    };
    if (!confirm(labels[action])) return;
    const btn = document.querySelector(
        `[data-si-action="${action}"][data-si-id="${id}"]`);
    try {
        if (btn) {
            await withButtonBusy(btn, async () => {
                await securityIncidents[action](id);
            });
        } else {
            await securityIncidents[action](id);
        }
        const done = {
            confirm: `Security incident ${id} confirmed — deadline clocks set.`,
            notify: `Security incident ${id} marked notified.`,
            close: `Security incident ${id} closed.`,
        };
        if (window.showInfo) window.showInfo(done[action], 'Security incidents');
        _selectedId = id;
        await loadTable();
        await loadFollowUps();
    } catch (e) {
        if (btn) btn.disabled = false;
        _showError(_errText(e) || `Failed to update security incident ${id}`);
    }
}

// ---------------------------------------------------------------------------
// "New incident" form. Severity is the only required field (the API 422s
// otherwise); tenant_id NULL/empty = platform-wide incident.
// ---------------------------------------------------------------------------

async function handleCreate() {
    _clearError();
    const sev = _field('si-new-severity');
    const tenantRaw = _field('si-new-tenant');
    const summary = _field('si-new-summary');
    const detected = _field('si-new-detected');
    if (!sev) {
        _showError('Pick a severity (low|medium|high|critical) before registering an incident.');
        return;
    }
    const label = `Register a new security incident — severity ${sev}${summary ? `: ${summary}` : ' (no summary)'}${tenantRaw ? ` against tenant #${tenantRaw}` : ' platform-wide'}?`;
    if (!confirm(label)) return;
    try {
        const inc = await securityIncidents.create({
            tenant_id: tenantRaw ? Number(tenantRaw) : null,
            severity: sev,
            summary: summary || null,
            detected_at: detected || null,
        });
        if (window.showInfo) window.showInfo(`Security incident ${inc && inc.id ? inc.id : '(new)'} registered.`, 'Security incidents');
        await loadTable();
    } catch (e) {
        _showError(_errText(e) || 'Failed to register the security incident');
    }
}

function _field(id) {
    const el = document.getElementById(id);
    if (!el) return '';
    const v = el.value != null ? el.value : el.textContent;
    return String(v == null ? '' : v).trim();
}

// ---------------------------------------------------------------------------
// Follow-up panel for the selected incident (open + done rows, due_at order
// server-side). Add is behind confirm(); complete is behind confirm().
// ---------------------------------------------------------------------------

async function loadFollowUps() {
    const tbody = document.getElementById('si-fu-table-body');
    const empty = document.getElementById('si-fu-empty');
    if (!tbody) return;
    if (empty) empty.classList.add('hidden');
    if (!_selectedId) { tbody.innerHTML = ''; return; }
    tbody.innerHTML = '<tr><td colspan="5" class="px-4 py-4 text-gray-500">Loading…</td></tr>';
    try {
        const list = await securityIncidents.followUps(_selectedId);
        const items = (list && list.items) || [];
        if (!items.length) {
            tbody.innerHTML = '';
            if (empty) empty.classList.remove('hidden');
            return;
        }
        tbody.innerHTML = items.map(fuHtml).join('');
        tbody.querySelectorAll('[data-si-fu-action]').forEach(btn => {
            btn.addEventListener('click', () =>
                handleFollowUpComplete(btn.dataset.siFuIncident, Number(btn.dataset.siFuId)));
        });
    } catch (e) {
        tbody.innerHTML = '';
        _showError(_errText(e) || 'Failed to load follow-ups');
    }
}

function fuHtml(f) {
    const done = f.done_at != null;
    const actions = done
        ? `<span class="text-xs text-gray-400">done ${_fmt(f.done_at)}</span>`
        : `<button type="button" data-si-fu-action="complete" data-si-fu-incident="${_selectedId}" data-si-fu-id="${f.id}" class="text-green-700 hover:underline text-sm">Complete</button>`;
    return `<tr class="hover:bg-gray-50">
        <td class="px-4 py-3 font-mono text-xs">${f.id}</td>
        <td class="px-4 py-3">${esc(f.owner || '—')}</td>
        <td class="px-4 py-3">${esc(f.note || '—')}</td>
        <td class="px-4 py-3 whitespace-nowrap font-mono text-xs">${_fmt(f.due_at)}</td>
        <td class="px-4 py-3 text-right whitespace-nowrap">${actions}</td>
    </tr>`;
}

async function handleFollowUpAdd() {
    _clearError();
    if (!_selectedId) {
        _showError('Select an incident first (the follow-up panel follows the last incident you acted on).');
        return;
    }
    const owner = _field('si-fu-owner');
    const due = _field('si-fu-due');
    const note = _field('si-fu-note');
    if (!owner || !due) {
        _showError('A follow-up needs an owner and a due date.');
        return;
    }
    if (!confirm(`Add a follow-up for incident ${_selectedId} — ${owner}${note ? `: ${note}` : ''} due ${due}?`)) return;
    try {
        const res = await securityIncidents.addFollowUp(_selectedId, {
            owner, due_at: due, note: note || null,
        });
        if (window.showInfo) window.showInfo('Follow-up added.', 'Security incidents');
        await loadFollowUps();
        return res;
    } catch (e) {
        _showError(_errText(e) || 'Failed to add the follow-up');
    }
}

async function handleFollowUpComplete(incidentId, fuId) {
    _clearError();
    if (!confirm(`Mark follow-up #${fuId} on incident ${incidentId} complete?`)) return;
    const btn = document.querySelector(
        `[data-si-fu-action="complete"][data-si-fu-id="${fuId}"]`);
    try {
        if (btn) {
            await withButtonBusy(btn, async () => {
                await securityIncidents.completeFollowUp(incidentId, fuId);
            });
        } else {
            await securityIncidents.completeFollowUp(incidentId, fuId);
        }
        if (window.showInfo) window.showInfo(`Follow-up #${fuId} complete.`, 'Security incidents');
        await loadFollowUps();
    } catch (e) {
        if (btn) btn.disabled = false;
        _showError(_errText(e) || `Failed to complete follow-up #${fuId}`);
    }
}

async function handleCloseIncident(id) {
    return await handleAction('close', id);
}

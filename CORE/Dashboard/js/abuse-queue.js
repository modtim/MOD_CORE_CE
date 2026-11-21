// GSEC FX.93 p3 — Abuse-report queue page (platform admin only).
//
// Backend: routers/abuse_reports.py (/v1/admin/abuse/reports*).
//   GET  /admin/abuse/reports?status=&overdue_only=&limit=&offset= -> {total, items}
//   GET  /admin/abuse/reports/{id}
//   POST /admin/abuse/reports/{id}/triage {action, note} — action is one of
//        dismiss | suspend_target | actioned (TRIAGE_ACTIONS server-side);
//        only 'open' reports can be triaged (server 409s otherwise).
// Table columns come from the real DTO (services row_to_dict): id, category,
// reporter_email / reporter_user_id, target_type + target_id, status, created_at
// (received), triage_due_at + overdue (SLA: triage within 1 business day).
// CSAM/NCMEC runbook (docs/runbooks/csam-ncmec-reporting.md): reported content
// is NEVER rendered inline anywhere — this page shows metadata only (the
// reporter's own description text is not rendered at all).
import { abuseQueue, me } from './api.js';
import { withButtonBusy } from './utils.js';

let _statusFilter = null;    // open | dismissed | actioned (API `status` param)
let _overdueOnly = false;

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
    const box = document.getElementById('aq-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('aq-error');
    if (box) box.classList.add('hidden');
}

// SLA display from triage_due_at (server sets it = received + 1 business day).
function _slaInfo(row) {
    if (row.deleted_at) return { label: '—', cls: 'bg-gray-100 text-gray-500' };
    if (!row.triage_due_at) return { label: '—', cls: 'bg-gray-100 text-gray-500' };
    const due = new Date(row.triage_due_at);
    if (isNaN(due.getTime())) return { label: 'Date unreadable', cls: 'bg-amber-100 text-amber-700' };
    if (row.status !== 'open') return { label: `due ${_fmt(row.triage_due_at)}`, cls: 'bg-gray-100 text-gray-500' };
    const days = Math.ceil((due.getTime() - Date.now()) / 86400000);
    if (days < 0) return { label: `OVERDUE ${-days}d`, cls: 'bg-red-100 text-red-700' };
    if (days === 0) return { label: 'Due today', cls: 'bg-amber-100 text-amber-700' };
    return { label: `Due in ${days}d`, cls: 'bg-green-100 text-green-700' };
}

// ---------------------------------------------------------------------------
// init + controls
// ---------------------------------------------------------------------------

export async function initializeAbuseQueuePage() {
    ['aq-filter-open', 'aq-filter-dismissed', 'aq-filter-actioned'].forEach(id => {
        const btn = document.getElementById(id);
        if (!btn || btn.dataset.wired) return;
        btn.dataset.wired = '1';
        const status = id.replace('aq-filter-', '');
        btn.addEventListener('click', () => {
            _statusFilter = (_statusFilter === status) ? null : status;
            btn.classList.toggle('bg-slate-200');
            loadTable();
        });
    });
    const overdueToggle = document.getElementById('aq-overdue-toggle');
    if (overdueToggle && !overdueToggle.dataset.wired) {
        overdueToggle.dataset.wired = '1';
        overdueToggle.addEventListener('click', () => {
            _overdueOnly = !_overdueOnly;
            overdueToggle.classList.toggle('bg-red-100');
            loadTable();
        });
    }
    await loadTable();
}

async function loadTable() {
    _clearError();
    const tbody = document.getElementById('aq-table-body');
    const empty = document.getElementById('aq-empty');
    if (!tbody) return;
    if (empty) empty.classList.add('hidden');
    tbody.innerHTML = '<tr><td colspan="8" class="px-4 py-6 text-gray-500">Loading…</td></tr>';
    try {
        const [list, user] = await Promise.all([
            abuseQueue.list({
                status: _statusFilter,
                overdue_only: _overdueOnly,
                limit: 500,
            }),
            me.get(),
        ]);
        if (user && user.is_platform_admin === false) {
            // The nav hides the page for non-admins; a deep link still must
            // not render the queue (the API 403s anyway — belt and braces).
            tbody.innerHTML = '';
            _showError('Abuse queue is platform-admin only.');
            return;
        }
        let items = (list && list.items) || [];
        const count = document.getElementById('aq-count');
        if (count) {
            const total = (list && list.total != null) ? list.total : items.length;
            const overdueN = items.filter(r => r.overdue === true).length;
            const bits = [`${total} report(s)`];
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
        tbody.querySelectorAll('[data-aq-action]').forEach(btn => {
            btn.addEventListener('click', () =>
                handleTriage(btn.dataset.aqAction, Number(btn.dataset.aqId)));
        });
    } catch (e) {
        tbody.innerHTML = '';
        _showError(_errText(e) || 'Failed to load the abuse queue');
    }
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

function rowHtml(r) {
    const sla = _slaInfo(r);
    const reporter = r.reporter_email
        ? `${esc(r.reporter_email)}`
        : (r.reporter_user_id ? `user #${r.reporter_user_id}` : '—');
    const statusCls = r.status === 'open'
        ? 'bg-amber-100 text-amber-800'
        : (r.status === 'actioned' ? 'bg-green-100 text-green-700' : 'bg-gray-200 text-gray-600');
    // CSAM/NCMEC runbook: metadata only — the report `description` is NOT
    // rendered (it is reporter-authored text about reported content).
    const actions = (r.deleted_at || r.status !== 'open')
        ? `<span class="text-xs text-gray-400">${r.deleted_at ? `deleted ${_fmt(r.deleted_at)}` : 'closed'}</span>`
        : [
            `<button type="button" data-aq-action="dismiss" data-aq-id="${r.id}" class="text-gray-600 hover:underline text-sm">Dismiss</button>`,
            `<button type="button" data-aq-action="suspend_target" data-aq-id="${r.id}" class="text-red-700 hover:underline text-sm">Suspend target</button>`,
            `<button type="button" data-aq-action="actioned" data-aq-id="${r.id}" class="text-green-700 hover:underline text-sm">Actioned</button>`,
        ].join(' ');
    return `<tr class="hover:bg-gray-50">
        <td class="px-4 py-3 font-mono text-xs">${r.id}</td>
        <td class="px-4 py-3 whitespace-nowrap"><span class="px-2 py-0.5 rounded-full text-xs bg-red-50 text-red-700">${esc(r.category || '—')}</span></td>
        <td class="px-4 py-3">${esc(reporter)}</td>
        <td class="px-4 py-3 font-mono text-xs">${esc(`${r.target_type || '?'} #${r.target_id || '?'}`)}</td>
        <td class="px-4 py-3 whitespace-nowrap"><span class="px-2 py-0.5 rounded-full text-xs ${statusCls}">${esc(r.status || '—')}</span></td>
        <td class="px-4 py-3 whitespace-nowrap">${_fmt(r.created_at)}</td>
        <td class="px-4 py-3 whitespace-nowrap"><span class="px-2 py-0.5 rounded-full text-xs ${sla.cls}">${esc(sla.label)}</span></td>
        <td class="px-4 py-3 text-right whitespace-nowrap">${actions}</td>
    </tr>`;
}

// ---------------------------------------------------------------------------
// Triage actions (terminal: dismiss / suspend_target / actioned).
// All three move an open report out of the queue, so each gets a confirm
// step. suspend_target only RECORDS the request server-side (execution +
// appeal flow are a later FX.93 part) — the confirm text says so.
// ---------------------------------------------------------------------------

async function handleTriage(action, id) {
    _clearError();
    const labels = {
        dismiss: `Dismiss abuse report #${id} as not actionable? It moves to 'dismissed' and stops being triageable.`,
        suspend_target: `Record a suspension request against the target of abuse report #${id}? This RECORDS the request only — executing the suspension and the target appeal flow are a later step. The report moves to 'actioned'.`,
        actioned: `Mark abuse report #${id} as actioned (an enforcement action was taken)? It moves to 'actioned' and stops being triageable.`,
    };
    if (!confirm(labels[action])) return;
    const btn = document.querySelector(
        `[data-aq-action="${action}"][data-aq-id="${id}"]`);
    try {
        if (btn) {
            await withButtonBusy(btn, async () => {
                await abuseQueue.triage(id, action);
            });
        } else {
            await abuseQueue.triage(id, action);
        }
        const done = {
            dismiss: `Abuse report #${id} dismissed.`,
            suspend_target: `Suspension request recorded for abuse report #${id}.`,
            actioned: `Abuse report #${id} marked actioned.`,
        };
        if (window.showInfo) window.showInfo(done[action], 'Abuse queue');
        await loadTable();
    } catch (e) {
        if (btn) btn.disabled = false;
        _showError(_errText(e) || `Failed to triage report #${id}`);
    }
}

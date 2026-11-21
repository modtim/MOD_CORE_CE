// FX.70 p3 — Tenant support tickets (GSEC FX.70, owner decision Q53=A).
// Backend: CORE/API/routers/tenant/support_tickets.py, mounted under /v1/tenant:
//   POST /support-tickets   body {severity: sev1|sev2|sev3|sev4,
//                                subject: 3..255 chars, body?: <=20000}
//                         -> ticket payload (+ auto-ack email server-side)
//   GET  /support-tickets   ?status=open|in_progress|resolved|closed
//                           &limit=1..200 &offset>=0
//                         -> {items: [ticket], total, limit, offset}
// ticket payload: id, tenant_id, opened_by, severity, subject, body,
//   status, plan_tier, first_response_due_at, first_responded_at,
//   first_responded_by, resolved_at, resolved_by, created_at, updated_at.
// Both routes are gated on require_active_and_onboarded_user ONLY (no role
// gate) — any signed-in, onboarded tenant user may open and read tickets.
// Respond/resolve/list-all are platform-admin routes elsewhere; this page is
// the tenant side only, and every read is filtered to this tenant server-side.
import { supportTickets, me } from './api.js';
import { withButtonBusy } from './utils.js';

let _stLoaded = false;

function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// 'YYYY-MM-DD HH:MM' (UTC, as the API sends ISO) — the first-response target
// is an SLA clock, so the time matters, not just the day.
function _fmt(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? '—'
        : `${d.toISOString().slice(0, 10)} ${d.toISOString().slice(11, 16)}`;
}

function _fmtDay(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 10);
}

function _showError(msg) {
    const box = document.getElementById('st-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('st-error');
    if (box) box.classList.add('hidden');
}

// Verbatim server detail (string as-is; FastAPI 422 arrays joined as
// "field: msg" pairs — same shape as account.js _formatScError).
function _errText(e) {
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

const SEVERITIES = ['sev1', 'sev2', 'sev3', 'sev4'];

function _severityBadge(sev) {
    const cls = sev === 'sev1' ? 'bg-red-100 text-red-700'
        : sev === 'sev2' ? 'bg-amber-100 text-amber-700'
        : 'bg-gray-100 text-gray-600';
    return `<span class="px-2 py-0.5 rounded-full text-xs ${cls}">${esc(sev || '—')}</span>`;
}

function _statusBadge(status) {
    const cls = status === 'open' ? 'bg-amber-100 text-amber-700'
        : status === 'in_progress' ? 'bg-blue-100 text-blue-700'
        : status === 'resolved' ? 'bg-green-100 text-green-700'
        : 'bg-gray-100 text-gray-600';
    return `<span class="px-2 py-0.5 rounded-full text-xs ${cls}">${esc(status || '—')}</span>`;
}

// ---------------------------------------------------------------------------
// init + load
// ---------------------------------------------------------------------------

export async function initializeSupportTicketsPage() {
    const btn = document.getElementById('st-create-save');
    if (btn) btn.addEventListener('click', handleCreate);
    await loadTickets();
}

async function loadTickets() {
    _clearError();
    const tbody = document.getElementById('st-ticket-body');
    const empty = document.getElementById('st-empty');
    if (!tbody) return;
    if (empty) empty.classList.add('hidden');
    tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-6 text-gray-500">Loading…</td></tr>';
    try {
        const [res, user] = await Promise.all([
            supportTickets.list(),
            me.get(),
        ]);
        // The router 404s "Tenant not found" for a user with no tenant_id;
        // the nav is visible to every signed-in user, so say so here rather
        // than rendering an empty table that looks like "no tickets".
        if (user && user.tenant_id == null) {
            tbody.innerHTML = '';
            _showError('Support tickets are tenant-scoped; this account has no tenant.');
            return;
        }
        const items = (res && res.items) || [];
        _stLoaded = true;
        const count = document.getElementById('st-count');
        if (count) {
            const openN = items.filter(t => t.status === 'open').length;
            const total = res && res.total != null ? res.total : items.length;
            count.textContent = `${items.length} shown · ${total} total · ${openN} open`;
        }
        if (!items.length) {
            tbody.innerHTML = '';
            if (empty) empty.classList.remove('hidden');
            return;
        }
        tbody.innerHTML = items.map(ticketRowHtml).join('');
    } catch (e) {
        tbody.innerHTML = '';
        _showError(_errText(e) || 'Failed to load this tenant’s support tickets');
    }
}

// ---------------------------------------------------------------------------
// Ticket rows: subject, severity, status, opened, first-response target,
// responded/resolved times. Community tier has no SLA -> 'best effort'.
// ---------------------------------------------------------------------------

function _responseCell(t) {
    if (t.first_responded_at) {
        return `${_fmt(t.first_responded_at)}${t.first_responded_by != null ? ` · by #${t.first_responded_by}` : ''}`;
    }
    if (t.first_response_due_at) return `due ${_fmt(t.first_response_due_at)}`;
    return 'best effort';
}

function ticketRowHtml(t) {
    const opened = _fmtDay(t.created_at);
    const resolved = t.resolved_at
        ? `${_fmtDay(t.resolved_at)}${t.resolved_by != null ? ` · by #${t.resolved_by}` : ''}`
        : '—';
    return `<tr class="hover:bg-gray-50">
        <td class="px-4 py-3 font-mono text-xs">${t.id}</td>
        <td class="px-4 py-3">${esc(t.subject || '—')}</td>
        <td class="px-4 py-3 whitespace-nowrap">${_severityBadge(t.severity)}</td>
        <td class="px-4 py-3 whitespace-nowrap">${_statusBadge(t.status)}</td>
        <td class="px-4 py-3 whitespace-nowrap">${opened}</td>
        <td class="px-4 py-3 whitespace-nowrap">${_responseCell(t)} · ${resolved}</td>
    </tr>`;
}

// ---------------------------------------------------------------------------
// Open a ticket (the form fields are exactly what the API takes; severity is
// a <select> holding the API's allowed values). withButtonBusy disables the
// submit button while a submit is in flight, so a double click cannot open
// two tickets.
// ---------------------------------------------------------------------------

async function handleCreate() {
    _clearError();
    const btn = document.getElementById('st-create-save');
    if (!_stLoaded) {
        _showError('Tickets are still loading — try again in a moment.');
        return;
    }
    try {
        let payload = null;
        await withButtonBusy(btn, async () => {
            const severity = document.getElementById('st-create-severity')?.value || '';
            const subject = (document.getElementById('st-create-subject')?.value || '').trim();
            const bodyRaw = (document.getElementById('st-create-body')?.value || '').trim();
            payload = await supportTickets.create({
                severity: severity,
                subject: subject,
                body: bodyRaw || null,
            });
        });
        if (payload && payload.id != null) {
            if (window.showInfo) {
                const due = payload.first_response_due_at
                    ? `first response due ${_fmt(payload.first_response_due_at)}`
                    : 'best-effort response (no SLA)';
                window.showInfo(`Ticket #${payload.id} opened · ${due}.`, 'Support');
            }
        }
        await loadTickets();
    } catch (e) {
        if (btn) btn.disabled = false;
        _showError(_errText(e) || 'Failed to open the support ticket');
    }
}

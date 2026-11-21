// Dashboard/js/change-gate-report.js
//
// 23.7/GO.8 s2 — System > Settings > Change-Gate Report sub-tab
// (platform-admin only). Read-only UI readout of the variant change-gate
// WEEKLY REPORT persisted by the weekly sweep (services/scheduler.py) as the
// append-only audit event VARIANT_CHANGE_GATE_WEEKLY_REPORT. The panel calls
// GET /v1/woodsman/wizard/variant-change-gate-weekly-report (routers/nodes_builds.py →
// node_builder/variant_test/change_gate.py:latest_weekly_report) and renders
// the report: window (since/until/week_key), total/passed/regressed counts,
// the regressed-variant list (before → after verdict/score) and the
// below-threshold list (verdict/best_score). Answers {"report": null} when
// no sweep has run yet — that renders as an empty-state message, not an
// error. Idempotent via _cgWired, same pattern as the sibling admin
// sub-tabs (js/model-cache.js).

import { fetchApi as apiCall } from './api.js';

let _cgWired = false;
let _cgBusy = false;

function _cgEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function _cgNum(v) {
    return v == null || isNaN(v) ? '--' : String(v);
}

function _cgVerdictScore(v) {
    if (!v) return '--';
    const verdict = v.verdict == null ? '?' : _cgEsc(v.verdict);
    const score = v.score == null ? null : Number(v.score);
    return score == null ? verdict : `${verdict} · ${score.toFixed(2)}`;
}

function _cgSetMsg(kind, text) {
    const el = document.getElementById('cg-msg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'text-sm mb-4 ' + (kind === 'error' ? 'text-red-600' : 'text-gray-500');
    el.classList.remove('hidden');
}

function _cgClearMsg() {
    const el = document.getElementById('cg-msg');
    if (el) { el.textContent = ''; el.classList.add('hidden'); }
}

function _cgErrorText(err) {
    const status = err?.status;
    if (status === 403) {
        return 'Change-gate report is platform-admin only.';
    }
    if (err?.detail && typeof err.detail === 'object') {
        return _cgEsc(err.detail.message || JSON.stringify(err.detail));
    }
    return _cgEsc(err?.message || 'Request failed');
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function _cgRenderReport(report) {
    const el = document.getElementById('cg-report');
    if (!el) return;
    if (!report) {
        el.innerHTML = `<div class="bg-white p-5 rounded-lg border border-gray-200 shadow-soft">
            <p class="text-sm text-gray-500">No weekly report yet — the change-gate weekly sweep
            has not persisted a report in the last 365 days. It runs automatically;
            press Refresh after the next sweep.</p>
        </div>`;
        return;
    }

    const w = report.window || {};
    const regressed = (report.regressed_variants || []).map((r) =>
        `<tr>
            <td class="px-4 py-3 font-mono text-sm text-gray-800">${_cgEsc(r.node_kind)}</td>
            <td class="px-4 py-3 font-mono text-sm text-gray-600">${_cgEsc(r.variant)}</td>
            <td class="px-4 py-3 text-sm text-gray-600">${_cgEsc(r.reason)}</td>
            <td class="px-4 py-3 text-sm text-gray-600">${_cgVerdictScore(r.before)}</td>
            <td class="px-4 py-3 text-sm text-gray-600">${_cgVerdictScore(r.after)}</td>
        </tr>`).join('');

    const below = (report.below_threshold || []).map((r) =>
        `<tr>
            <td class="px-4 py-3 font-mono text-sm text-gray-800">${_cgEsc(r.node_kind)}</td>
            <td class="px-4 py-3 font-mono text-sm text-gray-600">${_cgEsc(r.variant)}</td>
            <td class="px-4 py-3 text-sm text-gray-600">${_cgEsc(r.reason)}</td>
            <td class="px-4 py-3 text-sm text-gray-600">${_cgEsc(r.verdict)}</td>
            <td class="px-4 py-3 text-sm text-gray-600">${r.best_score == null ? '--' : Number(r.best_score).toFixed(2)}</td>
        </tr>`).join('');

    const table = (title, body) => `<div class="bg-white rounded-lg shadow-soft border border-gray-200 overflow-hidden mb-4">
        <div class="px-4 py-3 border-b border-gray-200">
            <p class="text-sm font-medium text-gray-700">${title}</p>
        </div>
        <table class="min-w-full text-sm">
            <thead class="bg-gray-50">
                <tr>
                    <th class="px-4 py-3 text-left text-xs font-semibold text-gray-700 uppercase tracking-wide">Node kind</th>
                    <th class="px-4 py-3 text-left text-xs font-semibold text-gray-700 uppercase tracking-wide">Variant</th>
                    <th class="px-4 py-3 text-left text-xs font-semibold text-gray-700 uppercase tracking-wide">Reason</th>
                    <th class="px-4 py-3 text-left text-xs font-semibold text-gray-700 uppercase tracking-wide">Before / verdict</th>
                    <th class="px-4 py-3 text-left text-xs font-semibold text-gray-700 uppercase tracking-wide">After / best score</th>
                </tr>
            </thead>
            <tbody class="bg-white divide-y divide-gray-200">
                ${body || `<tr><td colspan="5" class="px-4 py-6 text-center text-sm text-gray-500">None this window.</td></tr>`}
            </tbody>
        </table>
    </div>`;

    el.innerHTML = `
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
            ${_cgStatTile('Window', `${_cgEsc(w.week_key) || '--'}`, `${_cgEsc(w.since)} → ${_cgEsc(w.until)}`)}
            ${_cgStatTile('Affected', _cgNum(report.total), 'variants touched by change gate')}
            ${_cgStatTile('Passed', _cgNum(report.passed), '')}
            ${_cgStatTile('Regressed', `<span class="${(report.regressed ?? 0) > 0 ? 'text-red-600' : 'text-success'}">${_cgNum(report.regressed)}</span>`, '')}
        </div>
        ${table('Regressed variants (score drop past threshold / good→bad)', regressed)}
        ${table('Below threshold (bad verdict / low score, not new regressions)', below)}`;
}

function _cgStatTile(label, value, sub) {
    return `<div class="bg-white p-5 rounded-lg border border-gray-200 shadow-soft">
        <p class="text-gray-500 text-xs font-medium">${_cgEsc(label)}</p>
        <p class="text-2xl font-bold text-gray-800 mt-2">${value}</p>
        ${sub ? `<p class="text-xs text-gray-400 mt-1">${sub}</p>` : ''}
    </div>`;
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function _cgLoad() {
    if (_cgBusy) return;
    _cgBusy = true;
    _cgClearMsg();
    try {
        const payload = await apiCall('/woodsman/wizard/variant-change-gate-weekly-report');
        _cgRenderReport(payload?.report);
    } catch (err) {
        const e = document.getElementById('cg-report');
        if (e) e.innerHTML = `<div class="text-sm text-red-600">${_cgErrorText(err)}</div>`;
        _cgSetMsg('error', _cgErrorText(err));
    } finally {
        _cgBusy = false;
    }
}

// ---------------------------------------------------------------------------
// Init (idempotent)
// ---------------------------------------------------------------------------

export function initializeChangeGateReportPanel() {
    _cgLoad();
    if (_cgWired) return;
    _cgWired = true;
    document.getElementById('cg-refresh')?.addEventListener('click', () => _cgLoad());
}

// GSEC FX.163 p3b2a — Tenant full export page (MOD Core Dashboard): the UI half of the
// self-serve full-tenant export (GSEC FX.59 part 3, owner decision Q42=A). Built as a
// STANDALONE page: a later job folds it into a hub tab, so nothing here touches ui.js,
// dashboard-shell.html or the hub files.
//
// Backend (CORE/API/routers/tenant/export.py, mounted at /v1/tenant — the API is /v1-only, so
// every path below is written WITHOUT a /v1 prefix, exactly like the neighbour pages):
//   POST /tenant/export   start the zip (workflows, files, outputs, audit log)
//                         201 -> {job_id, status, object_key, size_bytes, expires_at, error,
//                                 created_at, completed_at, download_url}
//                         202 -> {status:'awaiting_approval', approval_id} while the tenant's
//                         approvals.require_second_approver switch is ON (js/approval-pending.js)
//                         409 while a job is already in flight for this tenant
//   GET  /tenant/export   {jobs:[payload]} newest first; download_url is a 24 h presigned link
//                         present ONLY on a completed, unexpired job (each link handed out
//                         writes a TENANT_EXPORT_DOWNLOADED audit row — the server's decision)
//
// Statuses (models.TenantExportJobStatus): pending / building / uploading are RUNNING;
// completed / failed are terminal.
//
// POLLING: never a fixed setInterval (the WB poller self-DDoS history, and the poller-self-heal
// note). One setTimeout chain, 5 s, doubling to a 60 s cap, and it STOPS when no job is running
// or when the tab is hidden — the wizard-queue / storage-connections idiom.
//
// Every server string (status, error, ids, urls) reaches the DOM through textContent /
// setAttribute ONLY — the markup-assignment property is never touched, so a hostile error
// string cannot smuggle markup.

import { fetchApi } from './api.js';
import { isAwaitingApproval, showAwaitingApproval, clearAwaitingApproval } from './approval-pending.js';
import { withButtonBusy } from './utils.js';
import { me } from './api.js';

const PAGE_LIMIT = 10;
const POLL_BASE_MS = 5000;
const POLL_MAX_MS = 60000;
const RUNNING = ['pending', 'building', 'uploading'];

const APPROVAL_RUNNING = ['pending'];

const tenantExport = {
    start: () => fetchApi('/tenant/export', { method: 'POST' }),
    list: (limit = PAGE_LIMIT) => fetchApi(`/tenant/export?limit=${encodeURIComponent(String(limit))}`),
    // FX.161 p2b — the second-admin approval workflow (routers/tenant/export_approvals.py).
    approvals: () => fetchApi('/tenant/export-approvals'),
    approve: (requestId) => fetchApi(`/tenant/export-approvals/${encodeURIComponent(String(requestId))}/approve`,
        { method: 'POST' }),
    reject: (requestId, reason) => fetchApi(`/tenant/export-approvals/${encodeURIComponent(String(requestId))}/reject`,
        { method: 'POST', body: JSON.stringify({ reason: reason }) }),
};

let _jobs = [];
let _timer = null;
let _delayMs = POLL_BASE_MS;
let _polling = false;
let _startBusy = false;
let _visWired = false;
let _approvals = [];
let _approvalsBusy = false;
let _selfUserId = null;

function _errText(e) {
    if (!e) return '';
    const detail = e.detail !== undefined ? e.detail : e.message;
    if (Array.isArray(detail)) {
        return detail.map((x) => {
            if (typeof x === 'string') return x;
            if (x && typeof x === 'object') {
                const loc = Array.isArray(x.loc) ? x.loc.filter((p) => p !== 'body').join('.') : '';
                return loc ? `${loc}: ${x.msg || 'invalid'}` : (x.msg || JSON.stringify(x));
            }
            return String(x);
        }).join('; ');
    }
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object') {
        try { return detail.message || JSON.stringify(detail); } catch { return 'Unknown error'; }
    }
    return (e && e.message) || '';
}

function _showError(msg) {
    const box = document.getElementById('te-error');
    if (box) { box.textContent = msg || ''; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('te-error');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

function _showNotice(msg) {
    const box = document.getElementById('te-notice');
    if (box) { box.textContent = msg || ''; box.classList.remove('hidden'); }
}

function _clearNotice() {
    const box = document.getElementById('te-notice');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

function _fmt(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 19).replace('T', ' ');
}

function _sizeText(bytes) {
    if (bytes === null || bytes === undefined || bytes === '') return '—';
    let n = bytes;
    try { n = Number(bytes); } catch { return '—'; }
    if (!isFinite(n) || n < 0) return '—';
    if (n === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
    const v = n / Math.pow(1024, i);
    return `${i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

function _statusCls(status) {
    return status === 'completed' ? 'bg-green-100 text-green-700'
        : status === 'failed' ? 'bg-red-100 text-red-700'
        : status === 'pending' ? 'bg-amber-100 text-amber-800'
        : 'bg-gray-200 text-gray-600';
}

function _isRunning(job) {
    return !!job && RUNNING.includes(String(job.status || ''));
}

function _anyRunning(jobs) {
    return Array.isArray(jobs) && jobs.some(_isRunning);
}

export async function initializeTenantExportPage() {
    _wireVisibility();
    const btn = document.getElementById('te-start-btn');
    if (btn && !btn.dataset.wired) {
        btn.dataset.wired = '1';
        btn.addEventListener('click', () => { handleStart(); });
    }
    await load();
}

async function load() {
    _clearError();
    try {
        _jobs = await _fetchJobs();
    } catch (e) {
        _showError(_errText(e) || 'Failed to load the tenant export jobs');
        _jobs = [];
    }
    _lastJobs = _jobs;
    _renderAll(_jobs);
    await _loadApprovals();
    _schedulePoll();
}

async function _fetchJobs() {
    const res = await tenantExport.list(PAGE_LIMIT);
    return res && Array.isArray(res.jobs) ? res.jobs : [];
}

// The start. The API decides legality: 409 already-running / 403 no manage_settings grant /
// 202 held for a second approver. fetchApi does NOT throw on the 202, so the shared
// approval-pending module is what tells the user the action did not run.
async function handleStart() {
    if (_startBusy) return;
    _startBusy = true;
    const btn = document.getElementById('te-start-btn');
    _clearError();
    _clearNotice();
    clearAwaitingApproval(document.getElementById('te-approval-slot') || document);
    try {
        const resp = btn
            ? await withButtonBusy(btn, async () => tenantExport.start())
            : await tenantExport.start();
        if (isAwaitingApproval(resp)) {
            const slot = document.getElementById('te-approval-slot') || document;
            showAwaitingApproval(slot, resp, { actionLabel: 'Full export' });
            _showNotice('The export has NOT started yet — a different tenant admin must approve it, then start it again.');
        } else if (resp && resp.approval_required) {
            // FX.161 p2a's regulated-profile answer: the approval REQUEST was created
            // (one-shot, expires) but the export did not run. Show the API's own words.
            _showNotice(["Needs a second admin's approval — request sent.",
                resp.message ? String(resp.message) : ''].filter(Boolean).join(' '));
            await _loadApprovals();
        } else {
            const job = resp && resp.job_id != null ? resp : null;
            _showNotice(job
                ? `Export job #${job.job_id} started (status ${job.status || 'pending'}).`
                : 'Export started.');
            await load();
        }
    } catch (e) {
        _showError(_errText(e) || 'Failed to start the tenant export');
    } finally {
        _startBusy = false;
    }
}

// --- FX.161 p2b: the export-approvals section --------------------------------
function _approvalStatusCls(status) {
    return status === 'approved' ? 'bg-green-100 text-green-700'
        : status === 'rejected' ? 'bg-red-100 text-red-700'
        : status === 'expired' ? 'bg-gray-200 text-gray-600'
        : status === 'pending' ? 'bg-amber-100 text-amber-800'
        : 'bg-gray-200 text-gray-600';
}

function _approvalRunning(req) {
    return !!req && APPROVAL_RUNNING.includes(String(req.status || ''));
}

function _anyApprovalRunning(reqs) {
    return Array.isArray(reqs) && reqs.some(_approvalRunning);
}

// The requester's own row: the API 403s a self-approve, so the Approve button is
// hidden on it (the API's 403 text still shows in the row if a stale click lands).
function _isOwnRequest(req) {
    return !!req && _selfUserId != null && req.requested_by != null
        && String(req.requested_by) === String(_selfUserId);
}

function _clearApprovalsBody() {
    const body = document.getElementById('te-approvals-body');
    if (!body) return;
    while (body.firstChild) body.removeChild(body.firstChild);
}

function _approvalRow(req) {
    const st = req.status ? String(req.status) : 'unknown';
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-gray-50';
    const who = req.requested_by != null ? `user #${req.requested_by}` : '—';
    tr.appendChild(_td(who, 'px-4 py-3 whitespace-nowrap text-xs'));
    tr.appendChild(_td(_fmt(req.created_at), 'px-4 py-3 whitespace-nowrap text-xs'));
    tr.appendChild(_td(_fmt(req.expires_at), 'px-4 py-3 whitespace-nowrap text-xs'));
    const stTd = document.createElement('td');
    stTd.className = 'px-4 py-3 whitespace-nowrap';
    const chip = _badge(st, _approvalStatusCls(st));
    chip.setAttribute('aria-hidden', 'true');
    stTd.appendChild(chip);
    const stPlain = document.createElement('span');
    stPlain.className = 'text-xs text-gray-600 ml-2';
    stPlain.textContent = st;
    stTd.appendChild(stPlain);
    if (req.decision_reason) {
        const why = document.createElement('span');
        why.className = 'text-xs text-gray-600 ml-2';
        why.textContent = `reason: ${String(req.decision_reason)}`;
        stTd.appendChild(why);
    }
    if (req.status === 'expired') {
        const exp = document.createElement('span');
        exp.className = 'text-xs text-gray-500 ml-2';
        exp.textContent = 'Export approval request has expired';
        stTd.appendChild(exp);
    }
    tr.appendChild(stTd);
    const actTd = document.createElement('td');
    actTd.className = 'px-4 py-3 whitespace-nowrap';
    if (st === 'pending') {
        const own = _isOwnRequest(req);
        if (!own) {
            const ok = document.createElement('button');
            ok.type = 'button';
            ok.className = 'px-2 py-0.5 rounded-md text-xs border border-gray-300 text-gray-700 hover:bg-gray-100';
            ok.textContent = 'Approve';
            ok.addEventListener('click', () => { decideApproval(req, 'approve'); });
            actTd.appendChild(ok);
        } else {
            const note = document.createElement('span');
            note.className = 'text-xs text-gray-500';
            note.textContent = 'Your own request — a different tenant admin must decide it.';
            actTd.appendChild(note);
        }
        const no = document.createElement('button');
        no.type = 'button';
        no.className = 'px-2 py-0.5 rounded-md text-xs border border-gray-300 text-gray-700 hover:bg-gray-100';
        no.textContent = 'Reject';
        no.addEventListener('click', () => { decideApproval(req, 'reject'); });
        actTd.appendChild(no);
    } else {
        const done = document.createElement('span');
        done.className = 'text-xs text-gray-400';
        done.textContent = st === 'approved'
            ? 'approved — start the export once (it consumes this request)'
            : 'no action available';
        actTd.appendChild(done);
    }
    tr.appendChild(actTd);
    return tr;
}

function _renderApprovals(reqs) {
    _clearApprovalsBody();
    const body = document.getElementById('te-approvals-body');
    if (body) reqs.forEach((r) => { if (r) body.appendChild(_approvalRow(r)); });
    const empty = document.getElementById('te-approvals-empty');
    if (empty) {
        if (reqs.length) empty.classList.add('hidden');
        else empty.classList.remove('hidden');
    }
}

async function _loadApprovals() {
    try {
        const res = await tenantExport.approvals();
        _approvals = res && Array.isArray(res.requests) ? res.requests : [];
    } catch (e) {
        _approvals = [];
        const box = document.getElementById('te-reject-error');
        if (box) { box.textContent = _errText(e) || 'Failed to load export approval requests'; box.classList.remove('hidden'); }
    }
    try {
        const self = await me.get();
        _selfUserId = self && self.id != null ? self.id : null;
    } catch {
        _selfUserId = null;
    }
    _renderApprovals(_approvals);
}

async function decideApproval(req, decision) {
    if (_approvalsBusy) return;
    _approvalsBusy = true;
    const errBox = document.getElementById('te-reject-error');
    if (errBox) { errBox.textContent = ''; errBox.classList.add('hidden'); }
    let input = null;
    try {
        if (decision === 'reject') {
            input = document.getElementById('te-reject-reason');
            const reason = input ? String(input.value || '').trim() : '';
            if (!reason) {
                if (errBox) {
                    errBox.textContent = 'Type a reject reason in the field above — it is recorded in the audit trail.';
                    errBox.classList.remove('hidden');
                }
                return;
            }
            await tenantExport.reject(req.request_id, reason);
        } else {
            await tenantExport.approve(req.request_id);
        }
        if (input && decision === 'reject') input.value = '';
        await load();
    } catch (e) {
        if (errBox) {
            errBox.textContent = _errText(e) || 'The approval decision failed';
            errBox.classList.remove('hidden');
        }
    } finally {
        _approvalsBusy = false;
    }
}

function _clearBody() {
    const body = document.getElementById('te-table-body');
    if (!body) return;
    // Children are removed one at a time — the markup-assignment property is never used.
    while (body.firstChild) body.removeChild(body.firstChild);
}

function _rowNode(job) {
    const st = job.status ? String(job.status) : 'unknown';
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-gray-50';
    const cells = [
        [job.job_id != null ? String(job.job_id) : '—', 'px-4 py-3 whitespace-nowrap font-mono text-xs'],
    ];
    cells.forEach((c) => tr.appendChild(_td(c[0], c[1])));
    // Status: a contrast-safe chip PLUS the same word as plain cell text (the neighbour pages'
    // idiom — a plain copy always exists for a screen reader / no-CSS render).
    const stTd = document.createElement('td');
    stTd.className = 'px-4 py-3 whitespace-nowrap';
    const chip = _badge(st, _statusCls(st));
    chip.setAttribute('aria-hidden', 'true');
    stTd.appendChild(chip);
    const stPlain = document.createElement('span');
    stPlain.className = 'text-xs text-gray-600 ml-2';
    stPlain.textContent = st;
    stTd.appendChild(stPlain);
    tr.appendChild(stTd);
    const rest = [
        [_fmt(job.created_at), 'px-4 py-3 whitespace-nowrap text-xs'],
        [_fmt(job.completed_at), 'px-4 py-3 whitespace-nowrap text-xs'],
        [_sizeText(job.size_bytes), 'px-4 py-3 whitespace-nowrap text-xs'],
        [_fmt(job.expires_at), 'px-4 py-3 whitespace-nowrap text-xs'],
    ];
    rest.forEach((c) => tr.appendChild(_td(c[0], c[1])));
    tr.appendChild(_downloadCell(job));
    return tr;
}

// The download link. The URL is server-supplied, so it goes through setAttribute (the browser
// serialises it as a quoted attribute value) and the label through textContent.
function _downloadCell(job) {
    const td = document.createElement('td');
    td.className = 'px-4 py-3 whitespace-nowrap';
    const url = job.download_url;
    if (url && typeof url === 'string') {
        const a = document.createElement('a');
        a.setAttribute('href', url);
        a.setAttribute('rel', 'noreferrer');
        a.textContent = 'Download export';
        td.appendChild(a);
    } else if (job.status === 'failed') {
        const why = document.createElement('span');
        why.className = 'text-xs text-gray-600';
        why.textContent = job.error ? String(job.error) : 'failed — no error text';
        td.appendChild(why);
    } else {
        const none = document.createElement('span');
        none.className = 'text-xs text-gray-400';
        none.textContent = job.status === 'completed'
            ? 'link expired or presign failed' : 'not ready';
        td.appendChild(none);
    }
    return td;
}

function _td(text, cls) {
    const td = document.createElement('td');
    td.className = cls;
    td.textContent = text == null ? '—' : String(text);
    return td;
}

function _badge(text, cls) {
    const span = document.createElement('span');
    span.className = `px-2 py-0.5 rounded-full text-xs ${cls}`;
    span.textContent = text;
    return span;
}

function _renderAll(jobs) {
    const body = document.getElementById('te-table-body');
    _clearBody();
    if (body) jobs.forEach((j) => { if (j) body.appendChild(_rowNode(j)); });
    const count = document.getElementById('te-updated');
    if (count) {
        count.textContent = `${jobs.length} export job(s) · `
            + `running ${jobs.filter(_isRunning).length} · read at ${new Date().toISOString().slice(11, 19)}`;
    }
    const empty = document.getElementById('te-empty');
    if (empty) {
        if (jobs.length) empty.classList.add('hidden');
        else empty.classList.remove('hidden');
    }
}

// Backoff poll: ONE setTimeout chain, never a fixed setInterval (the WB self-DDoS history).
// The FIRST tick is 5 s, each later one doubles, capped at 60 s. The chain STOPS — no further
// timer is scheduled — when no job is running, and it also does not tick while the tab is
// hidden. A visibilitychange listener resumes it when the tab comes back and a job is still
// running (poller-self-heal: a hard stop that only resumes on a manual refresh is the bug this
// module must not repeat).
let _lastJobs = [];

function _schedulePoll() {
    _clearTimer();
    if (!_anyRunning(_lastJobs) && !_anyApprovalRunning(_approvals)) { _delayMs = POLL_BASE_MS; _polling = false; return; }
    _polling = true;
    if (document.visibilityState === 'hidden') return;
    _timer = setTimeout(() => { _timer = null; void load(); }, _delayMs);
    _delayMs = Math.min(_delayMs * 2, POLL_MAX_MS);
}

function _clearTimer() {
    if (_timer) { clearTimeout(_timer); _timer = null; }
}

function _onVisibility() {
    if (document.visibilityState === 'hidden') { _clearTimer(); return; }
    if (_anyRunning(_lastJobs) || _anyApprovalRunning(_approvals)) { _clearTimer(); _schedulePoll(); }
}

function _wireVisibility() {
    if (_visWired) return;
    _visWired = true;
    document.addEventListener('visibilitychange', _onVisibility);
}

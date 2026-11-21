// CORE/Dashboard/js/wizard-queue.js
//
// Vanilla-JS port of the Wizard Queue contract (wizard-queue-redesign §7,
// GE.WQR.22) — the Dashboard's System -> Activity view over the unified
// WizardRun table (models.WizardRun). Same field mapping and endpoints as
// the WorkflowBuilder panel (CORE/WorkflowBuilder/lib/wizard-queue.ts,
// components/wizard-run-row.tsx, wizard-runs-section.tsx) — read those for
// the TS mirror of this contract.
//
// Endpoints (served under /v1 — API_URL already includes the /v1 prefix,
// see inject-config.sh — router: CORE/API/routers/wizard_queue.py):
//   GET  /wizard-queue                  list (role-scoped, paginated, 20/page)
//   GET  /wizard-queue/{id}/logs        persisted logs (poll fallback)
//   GET  /wizard-queue/{id}/logs/stream live tail (SSE)
//   POST /wizard-queue/{id}/cancel      HARD cancel (+ cascade to children)
//   POST /wizard-queue/{id}/retry       retry as a NEW linked run
//   POST /wizard-queue/{id}/dismiss     soft-delete one terminal run
//   POST /wizard-queue/clear-completed  soft-delete all terminal in-scope runs
//
// No setInterval storms (memory: wb-poll-backoff / poller-self-heal — the
// WB previously DDoSed the API with ~8 no-backoff setInterval loops). The
// list refresh here uses an exponential backoff poller (base 5s, max 60s,
// reset to base on success, self-heals rather than hard-stopping) and
// pauses entirely while the tab is hidden, resuming on visibilitychange.
// Live log tail prefers SSE and falls back to a 2s poll only when the
// stream errors or is unavailable — the API's documented fallback path.
//
// Exports: initializeWizardQueuePanel(rootEl), stopWizardQueuePanel().

import { fetchApi, me } from './api.js';

const PAGE_SIZE = 20;
const LIST_BASE_MS = 5000;
const LIST_MAX_MS = 60000;
const LIST_MAX_FAILURES = 5;
const LOG_POLL_MS = 2000;

const WIZARD_TYPE_LABEL = {
    node: 'Node Wizard',
    stack: 'Stack Wizard',
    'agent-project': 'Agent / Project',
    'build-repair': 'Build Repair',
    'container-adapter': 'Container Adapter',
};

// Brand token classes only (no off-brand literals) — see tokens.css /
// tailwind.config.js for the primary / success / warning / destructive /
// muted semantic scale.
const STATUS_BADGE_CLASS = {
    queued: 'bg-muted text-muted-foreground',
    'waiting-for-worker': 'bg-warning/15 text-warning',
    running: 'bg-primary/15 text-primary',
    verifying: 'bg-primary/15 text-primary',
    'waiting-for-user': 'bg-warning/15 text-warning',
    succeeded: 'bg-success/15 text-success',
    failed: 'bg-destructive/15 text-destructive',
    canceled: 'bg-muted text-muted-foreground',
};

const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'canceled']);

function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

function _formatDuration(seconds) {
    if (seconds == null) return '';
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
}

// Defensive client-side scope filter (D5/D15/D17), mirroring
// services/wizard_queue.py::can_access / scope_query server-side. The
// server already scopes the list query per role; this is belt-and-
// suspenders so a stale cache or a future bug can never render a run
// outside the caller's own scope — platform admin sees platform-level
// runs only, a tenant admin sees their tenant's non-platform runs, and a
// regular member sees only their own runs.
export function isOwnScope(run, currentUser) {
    if (!run || !currentUser) return false;
    if (currentUser.is_platform_admin) return !!run.is_platform_level;
    if (run.is_platform_level) return false;
    if (currentUser.parent_user_id == null) {
        // tenant admin — same tenant only
        return run.tenant_id != null && run.tenant_id === currentUser.tenant_id;
    }
    // regular tenant member — own runs only
    return run.owner_user_id != null && run.owner_user_id === currentUser.id;
}

// --- Resilient list-refresh poller -----------------------------------
// Same semantics as CORE/WorkflowBuilder/lib/poller.ts's usePollingBackoff:
// exponential backoff capped at maxMs, reset to baseMs on success, pause
// while the tab is hidden (resume on visibility), and self-heal rather
// than hard-stop after repeated failures (keep probing at maxMs so the
// panel recovers the moment the API comes back, instead of freezing until
// a manual refresh).
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
        if (document.visibilityState !== 'hidden') {
            this._tick();
        }
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
        if (document.visibilityState === 'hidden') {
            this._clearTimer();
        } else {
            this._clearTimer();
            this._tick();
        }
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
            // After maxFailures, keep probing at the max cadence forever
            // rather than stopping — the memory note poller-self-heal
            // lesson: a hard stop only ever resumed on tab hide/show or
            // network `online`, which froze a focused tab through any API
            // blip until a manual refresh.
            this._schedule(this.failures >= this.maxFailures ? this.maxMs : this.curDelay);
        } finally {
            this.busy = false;
        }
    }
}

// --- Module state (single panel instance at a time) -------------------
let _rootEl = null;
let _currentUser = null;
let _offset = 0;
let _listPoller = null;
let _destroyed = true;
const _openLogs = new Map(); // runId -> { es, pollTimer, lastSeq, panelEl }

export function initializeWizardQueuePanel(rootEl) {
    if (!rootEl) return;
    stopWizardQueuePanel(); // idempotent teardown of any previous mount
    _rootEl = rootEl;
    _destroyed = false;
    _offset = 0;
    _currentUser = null;
    _rootEl.innerHTML = _shellHtml();
    _wireShellEvents();
    me.get().then(u => { _currentUser = u; }).catch(() => { _currentUser = null; });
    _listPoller = new BackoffPoller(() => _refreshList(), {
        baseMs: LIST_BASE_MS, maxMs: LIST_MAX_MS, maxFailures: LIST_MAX_FAILURES,
    });
    _listPoller.start();
}

export function stopWizardQueuePanel() {
    _destroyed = true;
    if (_listPoller) { _listPoller.stop(); _listPoller = null; }
    _closeAllLogs();
    _rootEl = null;
}

function _shellHtml() {
    return `
    <div class="wizard-queue-panel">
      <div class="flex items-center justify-between mb-4">
        <h2 class="text-lg font-semibold text-gray-800">Wizard Queue</h2>
        <div class="flex items-center gap-2">
          <button id="wq-clear-completed" class="px-3 py-1 text-xs font-medium text-white bg-primary rounded-lg hover:bg-primary/90">Clear completed</button>
          <button id="wq-refresh" class="px-3 py-1 text-xs font-medium border border-border rounded-lg hover:bg-muted">Refresh</button>
        </div>
      </div>
      <p class="text-sm text-muted-foreground mb-4">Live and recent runs across every wizard (Node Wizard, Stack Wizard, AI Assistant Agent/Project, Build Repair, Container Adapter) — observational only.</p>
      <div id="wq-rows" class="space-y-3"><p class="text-sm text-muted-foreground">Loading…</p></div>
      <div id="wq-pagination" class="flex items-center justify-between mt-4 text-xs text-muted-foreground">
        <button id="wq-prev" class="px-2 py-1 border border-border rounded disabled:opacity-40" disabled>Prev</button>
        <span id="wq-page-info"></span>
        <button id="wq-next" class="px-2 py-1 border border-border rounded disabled:opacity-40" disabled>Next</button>
      </div>
    </div>`;
}

function _wireShellEvents() {
    if (!_rootEl) return;
    _rootEl.querySelector('#wq-rows')?.addEventListener('click', _onRowsClick);
    _rootEl.querySelector('#wq-refresh')?.addEventListener('click', () => { _refreshList().catch(() => {}); });
    _rootEl.querySelector('#wq-clear-completed')?.addEventListener('click', () => { _onClearCompleted(); });
    _rootEl.querySelector('#wq-prev')?.addEventListener('click', () => {
        _offset = Math.max(0, _offset - PAGE_SIZE);
        _refreshList().catch(() => {});
    });
    _rootEl.querySelector('#wq-next')?.addEventListener('click', () => {
        _offset += PAGE_SIZE;
        _refreshList().catch(() => {});
    });
}

async function _refreshList() {
    if (_destroyed || !_rootEl) return;
    const resp = await fetchApi(`/wizard-queue?limit=${PAGE_SIZE}&offset=${_offset}`);
    if (_destroyed || !_rootEl) return; // torn down while the request was in flight
    const allRows = (resp && resp.runs) || [];
    // Filter defensively only once we know who the caller is; before that
    // resolves, trust the server's own scoping for the very first paint.
    const rows = _currentUser ? allRows.filter(r => isOwnScope(r, _currentUser)) : allRows;
    _renderRows(rows, (resp && resp.total_count) || 0);
}

function _renderRows(rows, totalCount) {
    if (!_rootEl) return;
    const rowsEl = _rootEl.querySelector('#wq-rows');
    if (!rowsEl) return;
    rowsEl.innerHTML = rows.length
        ? rows.map(_renderRow).join('')
        : '<p class="text-sm text-muted-foreground">No wizard runs.</p>';

    const pageInfo = _rootEl.querySelector('#wq-page-info');
    const prevBtn = _rootEl.querySelector('#wq-prev');
    const nextBtn = _rootEl.querySelector('#wq-next');
    const start = totalCount === 0 ? 0 : _offset + 1;
    const end = Math.min(_offset + PAGE_SIZE, totalCount);
    if (pageInfo) pageInfo.textContent = `${start}-${end} of ${totalCount}`;
    if (prevBtn) prevBtn.disabled = _offset === 0;
    if (nextBtn) nextBtn.disabled = _offset + PAGE_SIZE >= totalCount;

    // Tear down any open log stream for a run that fell off the current
    // page (retried, dismissed, or paged away) so we never keep tailing a
    // row that isn't rendered.
    const visibleIds = new Set(rows.map(r => r.id));
    Array.from(_openLogs.keys()).forEach(id => { if (!visibleIds.has(id)) _closeLogStream(id); });
}

// A wizard run that failed because no model endpoint is configured gets a
// clear "add a model endpoint" message with a Settings link instead of the
// raw error. The needle is the exact substring the API puts in
// error_message (support_chat_shared_service.py); every other error keeps
// rendering raw, escaped, as before.
const NO_MODEL_NEEDLE = 'no endpoint is configured for the wizard tool';
function _isNoModelError(msg) { return typeof msg === 'string' && msg.indexOf(NO_MODEL_NEEDLE) !== -1; }

function _renderRow(run) {
    const typeLabel = WIZARD_TYPE_LABEL[run.wizard_type] || run.wizard_type;
    const badgeCls = STATUS_BADGE_CLASS[run.status] || 'bg-muted text-muted-foreground';
    const target = escapeHtml(run.target_name || '(untitled)');
    const started = run.started_at ? new Date(run.started_at).toLocaleString() : '—';
    const duration = _formatDuration(run.duration_seconds);
    const runId = escapeHtml(run.id);

    const retryBackLink = run.retry_of_id
        ? `<div class="text-xs text-muted-foreground mt-1">retry of <span class="font-mono">${escapeHtml(run.retry_of_id)}</span></div>`
        : '';
    const errorLine = _isNoModelError(run.error_message)
        ? `<div class="text-xs text-destructive mt-1" data-testid="wizard-no-model">The node wizard needs a model. <a href="#settings" class="wq-ai-config-link underline" data-settings-tab="ai">Add a model endpoint in Settings &gt; AI Configuration</a> to turn it on.</div>`
        : run.error_message
        ? `<div class="text-xs text-destructive mt-1">${escapeHtml(run.error_message)}</div>`
        : '';
    const progressBar = !TERMINAL_STATUSES.has(run.status)
        ? `<div class="w-full h-1.5 bg-muted rounded-full mt-2 overflow-hidden"><div class="h-full bg-primary" style="width:${Math.max(0, Math.min(100, run.progress || 0))}%"></div></div>`
        : '';

    const cancelBtn = run.can_cancel
        ? `<button class="wq-cancel-btn px-2 py-1 text-xs font-medium text-destructive border border-destructive/30 rounded hover:bg-destructive/10" data-run-id="${runId}">Cancel</button>`
        : '';
    const retryBtn = run.can_retry
        ? `<button class="wq-retry-btn px-2 py-1 text-xs font-medium text-primary border border-primary/30 rounded hover:bg-primary/10" data-run-id="${runId}">Retry</button>`
        : '';
    const dismissBtn = TERMINAL_STATUSES.has(run.status)
        ? `<button class="wq-dismiss-btn px-2 py-1 text-xs font-medium text-muted-foreground border border-border rounded hover:bg-muted" data-run-id="${runId}">Dismiss</button>`
        : '';

    return `
      <div class="wq-row border border-border rounded-lg p-4 bg-white" data-testid="wizard-run-row" data-run-id="${runId}">
        <div class="flex items-start justify-between gap-3 flex-wrap">
          <div class="min-w-0">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="text-xs font-medium text-muted-foreground">${escapeHtml(typeLabel)}</span>
              <span class="wq-status-badge px-2 py-0.5 rounded-full text-xs font-medium ${badgeCls}" data-status="${escapeHtml(run.status)}">${escapeHtml(run.status)}</span>
            </div>
            <div class="text-sm font-medium text-gray-800 mt-1 break-words">${target}</div>
            <div class="text-xs text-muted-foreground mt-1">Started ${started}${duration ? ` · ${duration}` : ''}</div>
            ${errorLine}
            ${retryBackLink}
            ${progressBar}
          </div>
          <div class="flex flex-col items-end gap-2 shrink-0">
            <div class="flex items-center gap-2">
              ${cancelBtn}${retryBtn}${dismissBtn}
            </div>
            <button class="wq-logs-toggle text-xs text-primary hover:underline" data-run-id="${runId}">Logs</button>
          </div>
        </div>
        <div class="wq-log-panel hidden mt-3 bg-gray-900 text-gray-100 text-xs font-mono rounded p-3 max-h-48 overflow-y-auto"></div>
      </div>`;
}

// Deep link to one Settings tab: same replaceState-then-popstate form as
// _gotoPersistentStackRow in settings.js (replaceState fires no popstate of
// its own; ui.js listens on popstate and routes on window.location.hash).
function _gotoSettingsTab(tab) {
    const p = new URLSearchParams();
    p.set('tab', tab);
    window.history.replaceState(null, '',
        `${window.location.pathname}?${p.toString()}#settings`);
    window.dispatchEvent(new Event('popstate'));
}

function _onRowsClick(e) {
    const aiLink = e.target.closest('[data-settings-tab]');
    if (aiLink) { e.preventDefault(); return _gotoSettingsTab(aiLink.dataset.settingsTab); }
    const cancelBtn = e.target.closest('.wq-cancel-btn');
    if (cancelBtn) return _onCancel(cancelBtn.dataset.runId);
    const retryBtn = e.target.closest('.wq-retry-btn');
    if (retryBtn) return _onRetry(retryBtn.dataset.runId);
    const dismissBtn = e.target.closest('.wq-dismiss-btn');
    if (dismissBtn) return _onDismiss(dismissBtn.dataset.runId);
    const logsBtn = e.target.closest('.wq-logs-toggle');
    if (logsBtn) return _onToggleLogs(logsBtn);
}

async function _onCancel(runId) {
    if (!runId) return;
    try {
        await fetchApi(`/wizard-queue/${encodeURIComponent(runId)}/cancel`, { method: 'POST' });
    } catch (e) {
        console.warn('Wizard run cancel failed:', e);
    }
    _refreshList().catch(() => {});
}

async function _onRetry(runId) {
    if (!runId) return;
    try {
        await fetchApi(`/wizard-queue/${encodeURIComponent(runId)}/retry`, { method: 'POST' });
    } catch (e) {
        console.warn('Wizard run retry failed:', e);
    }
    _refreshList().catch(() => {});
}

async function _onDismiss(runId) {
    if (!runId) return;
    try {
        await fetchApi(`/wizard-queue/${encodeURIComponent(runId)}/dismiss`, { method: 'POST' });
    } catch (e) {
        console.warn('Wizard run dismiss failed:', e);
    }
    _refreshList().catch(() => {});
}

async function _onClearCompleted() {
    try {
        await fetchApi('/wizard-queue/clear-completed', { method: 'POST' });
    } catch (e) {
        console.warn('Wizard queue clear-completed failed:', e);
    }
    _offset = 0;
    _refreshList().catch(() => {});
}

function _onToggleLogs(btn) {
    const runId = btn.dataset.runId;
    if (!runId) return;
    const row = btn.closest('.wq-row');
    const panel = row ? row.querySelector('.wq-log-panel') : null;
    if (!panel) return;
    const isOpen = !panel.classList.contains('hidden');
    if (isOpen) {
        panel.classList.add('hidden');
        _closeLogStream(runId);
        btn.textContent = 'Logs';
    } else {
        panel.classList.remove('hidden');
        _openLogStream(runId, panel);
        btn.textContent = 'Hide logs';
    }
}

/** POST /stream-tickets {scope: "wizard-run", run_id} -> raw ticket, or null. */
async function _mintWizardLogStreamTicket(runId) {
    try {
        const resp = await fetchApi('/stream-tickets', {
            method: 'POST',
            body: JSON.stringify({ scope: 'wizard-run', run_id: runId }),
        });
        return (resp && typeof resp.ticket === 'string') ? resp.ticket : null;
    } catch (e) {
        console.debug('Wizard log stream ticket mint failed (non-fatal):', e);
        return null;
    }
}

// --- Live log tail: SSE with a 2s poll fallback ------------------------
// WS5.34b: auth is a short-lived, single-use `?ticket=` (minted via
// POST /stream-tickets) instead of the full access token — EventSource
// still can't set headers, but a ticket means nothing outside this
// process, so nothing sensitive lands in proxy access logs. A fresh
// ticket is minted for EACH connect attempt (a ticket is single-use, so
// the fallback-to-poll-on-error below is what stands in for "reconnect"
// here rather than letting EventSource replay a dead ticket).
async function _openLogStream(runId, panelEl) {
    if (_openLogs.has(runId)) return;
    panelEl.textContent = 'Connecting…';
    const state = { es: null, pollTimer: null, lastSeq: -1, panelEl };
    _openLogs.set(runId, state);
    panelEl.textContent = '';

    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    if (typeof EventSource !== 'undefined') {
        const ticket = await _mintWizardLogStreamTicket(runId);
        // The panel may have been closed while the mint was in flight.
        if (!_openLogs.has(runId)) return;
        if (ticket) {
            try {
                const es = new EventSource(`${apiUrl}/wizard-queue/${encodeURIComponent(runId)}/logs/stream?ticket=${encodeURIComponent(ticket)}`);
                es.addEventListener('message', (ev) => {
                    try {
                        _appendLogLine(state, JSON.parse(ev.data));
                    } catch (parseErr) {
                        // malformed line — ignore, keep the stream alive
                    }
                });
                es.addEventListener('end', () => { _teardownStream(state); });
                es.onerror = () => {
                    // SSE dropped (network blip, proxy timeout, run finished
                    // mid-stream, …) — fall back to the mandatory 2s poll
                    // rather than let EventSource's own silent auto-retry loop
                    // replay this (now-consumed) ticket indefinitely.
                    _teardownStream(state);
                    if (_openLogs.has(runId)) _startLogPoll(runId, state);
                };
                state.es = es;
                return;
            } catch (initErr) {
                console.debug('Wizard log SSE init failed, falling back to poll:', initErr);
            }
        }
    }
    _startLogPoll(runId, state);
}

function _teardownStream(state) {
    if (state.es) {
        try { state.es.close(); } catch (closeErr) { /* best effort */ }
        state.es = null;
    }
}

function _startLogPoll(runId, state) {
    if (state.pollTimer) return;
    const poll = async () => {
        if (!_openLogs.has(runId)) return;
        try {
            const resp = await fetchApi(`/wizard-queue/${encodeURIComponent(runId)}/logs?after_seq=${state.lastSeq}`);
            ((resp && resp.lines) || []).forEach(row => _appendLogLine(state, row));
        } catch (e) {
            // stay silent — the panel keeps its last content; next tick retries.
        }
    };
    poll();
    state.pollTimer = setInterval(poll, LOG_POLL_MS);
}

function _appendLogLine(state, row) {
    if (!row) return;
    if (typeof row.seq === 'number') state.lastSeq = Math.max(state.lastSeq, row.seq);
    const div = document.createElement('div');
    div.className = 'wq-log-line';
    // textContent (not innerHTML) — the DOM API itself is the escape here,
    // safe against any characters a redacted-but-not-HTML-safe log message
    // might still contain.
    div.textContent = `[${row.level || 'info'}] ${row.message || ''}`;
    state.panelEl.appendChild(div);
    state.panelEl.scrollTop = state.panelEl.scrollHeight;
}

function _closeLogStream(runId) {
    const state = _openLogs.get(runId);
    if (!state) return;
    _teardownStream(state);
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    _openLogs.delete(runId);
}

function _closeAllLogs() {
    Array.from(_openLogs.keys()).forEach(_closeLogStream);
}

// logs.js
// Logs page: Workflow Runs, System Tasks, Log Streaming tabs. The Permission
// Audit tab was removed (BUG.B29b2) — the audit log lives in the Compliance
// hub's "Audit log" tab.

import { fetchApi as apiCall, me, isPaidEdition } from './api.js';
import { initAIChat, resetAIChat, setupAIChatListeners } from './ai-chat.js';
import { initLogsLLMPanel, resetLogsLLMPanel } from './logs-llm-panel.js';
import { isPlatformAdmin } from './utils.js';
import { setActiveTab, getActiveTab } from './tab-state.js';
import { initializeLogStreamsPage } from './log-streams.js';
// GROUP L 20.6.s3 — MOD Talk → Plane review panel (a TAB of this hub, never a new
// sidebar row). Its initializer fills #logs-panel-talk-to-task on first activation.
import { initializeTalkToTaskPanel } from './hub-talk-to-task.js';

let workflowPage = 0;
let workflowPageSize = 20;
let workflowFilters = {};
let currentUser = null;
let currentTaskLogs = null;
// Fix A — plaintext log/summary for the currently-open Workflow Run Details
// modal (the view the Details button opens). Populated in
// viewWorkflowRunDetails and copied by the modal's "Copy Logs" button.
let currentRunDetailLog = null;
let autoRefreshInterval = null;
let logPollInterval = null;

// BUG.B28b — free-text search over workflow runs (id="runs-search"). The value
// feeds the `q` param on GET /road/runs (workflow name, who ran it, status,
// or run id). Null-guarded: the Logs page can unmount the panel mid-load (BUG.4).
function getRunsSearchValue() {
    const el = document.getElementById('runs-search');
    return el ? String(el.value || '').trim() : '';
}

// 0b.2 redo (R37.4 part 2) — `initialTab` lets ui.js land an old #log-streams
// bookmark on the Logs page's Log Streaming tab without a ?tab= query string
// (same shape as initializeAnalyticsPage's initialTab).
export async function initializeLogsPage(initialTab = null) {
    console.log('Initializing Logs page');

    // Check user role first (must complete before loading data)
    await checkUserRole();

    // rc20-ce-nav (owner 11-04) — Plane is an app ON TOP of MOD, not a page inside
    // MOD: the "Talk → Plane" review tab now ships hidden in pages/logs.html and
    // is revealed ONLY on a POSITIVE paid edition (saas/pro). CE/unknown keep it
    // hidden from first paint, so it is never painted and then removed (no
    // flicker). The backing interventions router ships on CE (open-core/manifest
    // api_allow :172), so this hide is a product decision, not a paid carve —
    // paid-edition behaviour (tab visible) is unchanged.
    if (await isPaidEdition()) {
        const talkTab = document.getElementById('logs-tab-talk-to-task');
        if (talkTab) talkTab.classList.remove('hidden');
    }

    // Show System Tasks tab ONLY for platform admins. Tenant owners
    // (parent_user_id === null) used to see this tab too, but the
    // backing /workers/tasks/system-tasks endpoint is platform-admin
    // gated so the tab was rendering and then 403ing for them.
    if (isPlatformAdmin(currentUser)) {
        const systemTab = document.getElementById('logs-tab-system');
        if (systemTab) systemTab.style.display = '';
    }

    // 0b.2 redo (R37.4 part 2) — Log Streaming rides this page's tab strip
    // instead of its own sidebar entry. Same gate the old log-streams-nav-link
    // used in ui.js updateUIVisibility: (isSuperAdmin || isPlatformAdmin).
    // The button starts with the `hidden` class (not style display), so reveal
    // it by removing that class.
    if (await isPaidEdition() && (isPlatformAdmin(currentUser) || currentUser?.parent_user_id === null)) {
        const lsTab = document.getElementById('logs-tab-log-streams');
        if (lsTab) lsTab.classList.remove('hidden');
    }

    // Tab switching
    document.querySelectorAll('.logs-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            // Update tab styles
            document.querySelectorAll('.logs-tab').forEach(t => {
                t.classList.remove('border-primary', 'text-primary');
                t.classList.add('border-transparent', 'text-gray-500');
            });
            tab.classList.add('border-primary', 'text-primary');
            tab.classList.remove('border-transparent', 'text-gray-500');

            // Remember the active sub-tab so a refresh restores it (#20).
            setActiveTab(tab.id.replace('logs-tab-', ''));

            // Switch panels
            const panelId = tab.id.replace('logs-tab-', 'logs-panel-');
            document.querySelectorAll('.logs-panel').forEach(p => p.classList.add('hidden'));
            const panel = document.getElementById(panelId);
            if (panel) panel.classList.remove('hidden');

            // Load data for active panel
            if (panelId === 'logs-panel-workflows') {
                loadWorkflowRuns();
            } else if (panelId === 'logs-panel-system') {
                loadSystemTasks();
            } else if (panelId === 'logs-panel-log-streams') {
                loadLogStreamsPanel();
            } else if (panelId === 'logs-panel-talk-to-task') {
                // 20.6.s3 — MOD Talk → Plane review (its own initializer is
                // idempotent: it re-runs the list, never re-appends its boxes).
                initializeTalkToTaskPanel();
            }
        });
    });

    // Refresh button
    document.getElementById('refresh-logs-btn')?.addEventListener('click', () => {
        const activePanel = document.querySelector('.logs-panel:not(.hidden)');
        if (!activePanel) return;
        if (activePanel.id === 'logs-panel-workflows') {
            loadWorkflowRuns();
        } else if (activePanel.id === 'logs-panel-system') {
            loadSystemTasks();
        } else if (activePanel.id === 'logs-panel-log-streams') {
            // The shared Refresh button is a no-op here once the tab has loaded:
            // js/log-streams.js wires its own New/Cancel/submit listeners inside
            // the panel, and re-running its initializer would double-bind them.
            loadLogStreamsPanel();
        } else if (activePanel.id === 'logs-panel-talk-to-task') {
            initializeTalkToTaskPanel();
        }
    });

    // Workflow Runs filters and pagination
    document.getElementById('apply-workflow-filters')?.addEventListener('click', applyWorkflowFilters);
    document.getElementById('reset-workflow-filters')?.addEventListener('click', resetWorkflowFilters);
    document.getElementById('workflow-page-size')?.addEventListener('change', (e) => {
        workflowPageSize = parseInt(e.target.value);
        workflowPage = 0;
        loadWorkflowRuns();
    });
    document.getElementById('prev-workflow-page')?.addEventListener('click', () => {
        if (workflowPage > 0) {
            workflowPage--;
            loadWorkflowRuns();
        }
    });
    document.getElementById('next-workflow-page')?.addEventListener('click', () => {
        workflowPage++;
        loadWorkflowRuns();
    });

    // BUG.B28b — the runs search box applies on its own: Enter in the box = apply
    // at once, typing debounces 400 ms (one request per keystroke burst), changing
    // the search always returns to the first page, clearing it reloads unfiltered.
    const runsSearchInput = document.getElementById('runs-search');
    if (runsSearchInput) {
        let lastAppliedRunsSearch = null;
        let runsSearchDebounceTimer = null;

        const applyRunsSearch = () => {
            const q = getRunsSearchValue();
            if (q === lastAppliedRunsSearch) return;
            lastAppliedRunsSearch = q;
            workflowPage = 0;
            loadWorkflowRuns();
        };

        const scheduleRunsSearchApply = () => {
            if (runsSearchDebounceTimer !== null) {
                clearTimeout(runsSearchDebounceTimer);
            }
            runsSearchDebounceTimer = setTimeout(() => {
                runsSearchDebounceTimer = null;
                applyRunsSearch();
            }, 400);
        };

        runsSearchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                if (runsSearchDebounceTimer !== null) {
                    clearTimeout(runsSearchDebounceTimer);
                    runsSearchDebounceTimer = null;
                }
                applyRunsSearch();
            } else if (!e.key || e.key.length === 1) {
                scheduleRunsSearchApply();
            }
        });
        runsSearchInput.addEventListener('input', scheduleRunsSearchApply);
        runsSearchInput.addEventListener('paste', scheduleRunsSearchApply);
    }

    // Modal close buttons
    document.getElementById('close-workflow-detail-modal')?.addEventListener('click', () => {
        resetAIChat();
        resetLogsLLMPanel({
            panelSelector: '#workflow-llm-panel',
            messagesSelector: '#workflow-llm-messages',
            formSelector: '#workflow-llm-form',
            inputSelector: '#workflow-llm-input',
        });
        document.getElementById('workflow-detail-modal').classList.add('hidden');
    });

    // Setup AI chat listeners
    setupAIChatListeners();
    document.getElementById('close-task-logs-modal')?.addEventListener('click', closeTaskLogsModal);
    document.getElementById('close-task-logs-btn')?.addEventListener('click', closeTaskLogsModal);
    document.getElementById('download-task-logs-btn')?.addEventListener('click', downloadTaskLogs);
    document.getElementById('copy-task-logs-btn')?.addEventListener('click', copyTaskLogs);
    // Fix A — Copy Logs for the Workflow Run Details modal (the view the
    // Details button actually opens).
    document.getElementById('copy-workflow-detail-log-btn')?.addEventListener('click', copyRunDetailLog);

    // #20 — restore the sub-tab a refresh preserved in the URL. Only honour
    // a tab that exists and is visible (System Tasks is hidden for non-admins);
    // otherwise fall back to the Workflow Runs default.
    // 0b.2 redo (R37.4 part 2) — an explicit initialTab (ui.js deep link, e.g.
    // old #log-streams bookmarks) wins over the ?tab= param.
    // BUG.B29b2 — 'permissions' is no longer a tab here, so an old
    // ?tab=permissions / #logs?tab=permissions bookmark can't land anywhere:
    // ui.js redirects it to the Compliance hub, and if it still reaches this
    // page it falls through to the Workflow Runs default below.
    let wantedTab = initialTab || getActiveTab();
    if (wantedTab === 'permissions') wantedTab = null;
    const wantedTabEl = wantedTab ? document.getElementById(`logs-tab-${wantedTab}`) : null;
    if (wantedTabEl && wantedTabEl.style.display !== 'none'
        && !wantedTabEl.classList.contains('hidden') && wantedTab !== 'workflows') {
        wantedTabEl.click();
    } else {
        // Load Workflow Runs by default
        loadWorkflowRuns();
    }

    // Auto-refresh: check for changes every 10 seconds, only update if data changed
    if (autoRefreshInterval) clearInterval(autoRefreshInterval);
    autoRefreshInterval = setInterval(() => {
        const activePanel = document.querySelector('.logs-panel:not(.hidden)');
        if (activePanel && activePanel.id === 'logs-panel-workflows') {
            checkAndRefreshWorkflowRuns();
        }
    }, 10000);
}

async function checkUserRole() {
    try {
        currentUser = await me.get();
    } catch (error) {
        console.error('Error checking user role:', error);
    }
}

// ========================================
// Workflow Runs Tab
// ========================================

function applyWorkflowFilters() {
    workflowFilters = {
        status: document.getElementById('filter-workflow-status').value,
    };

    const dateRange = document.getElementById('filter-workflow-date').value;
    if (dateRange !== 'all') {
        const daysAgo = parseInt(dateRange);
        const startDate = new Date();
        startDate.setDate(startDate.getDate() - daysAgo);
        workflowFilters.start_date = startDate.toISOString();
    }

    workflowPage = 0;
    loadWorkflowRuns();
}

function resetWorkflowFilters() {
    document.getElementById('filter-workflow-status').value = '';
    document.getElementById('filter-workflow-date').value = '30';
    workflowFilters = {};
    workflowPage = 0;
    loadWorkflowRuns();
}

// Cache of last-known run states for smart refresh
let lastRunSnapshot = null;

async function checkAndRefreshWorkflowRuns() {
    try {
        let runs;
        runs = await apiCall(`/road/runs?limit=500&offset=0`);

        // Build a fingerprint: id + status for each run
        const fingerprint = (runs || []).map(r => {
            const id = r.id || r.dag_run_id || '';
            const status = r.status || r.state || '';
            return `${id}:${status}`;
        }).join('|');

        if (fingerprint !== lastRunSnapshot) {
            lastRunSnapshot = fingerprint;
            loadWorkflowRuns(true);  // silent = no "Loading..." flash
        }
    } catch (e) {
        // Silent — don't disrupt the user on a background poll failure
    }
}

async function loadWorkflowRuns(silent = false) {
    console.log('Loading Workflow Runs...');

    const tbody = document.getElementById('workflow-runs-tbody');
    if (!silent) {
        tbody.innerHTML = '<tr><td colspan="9" class="py-4 text-center text-gray-500">Loading...</td></tr>';
    }

    try {
        let runs = [];

        {
            // True server-side pagination. Request one extra row (pageSize+1):
            // the endpoint caps its return at `limit`, so receiving pageSize+1
            // rows means another page follows — a next-page probe without a
            // separate COUNT query. Status + date filters are applied server
            // side so pages span the entire filtered set.
            const params = new URLSearchParams({
                limit: workflowPageSize + 1,
                offset: workflowPage * workflowPageSize,
            });
            if (workflowFilters.status && workflowFilters.status !== '') {
                params.set('status_filter', workflowFilters.status);
            }
            if (workflowFilters.start_date) {
                params.set('start_date', workflowFilters.start_date);
            }
            // BUG.B28b — free-text search (workflow name, who ran it, status, run id).
            const runsSearch = getRunsSearchValue();
            if (runsSearch) {
                params.set('q', runsSearch);
            }
            runs = await apiCall(`/road/runs?${params.toString()}`);
        }

        tbody.innerHTML = '';

        const hasNext = (runs?.length || 0) > workflowPageSize;
        const pageRuns = (runs || []).slice(0, workflowPageSize);

        if (pageRuns.length === 0) {
            tbody.innerHTML = '<tr><td colspan="9" class="py-8 text-center text-gray-500">No workflow runs found</td></tr>';
            const info = document.getElementById('workflow-results-info');
            if (info) info.textContent =
                workflowPage > 0 ? `Page ${workflowPage + 1} — no results` : 'No results';
            const prev = document.getElementById('prev-workflow-page');
            if (prev) prev.disabled = workflowPage === 0;
            const next = document.getElementById('next-workflow-page');
            if (next) next.disabled = true;
            return;
        }

        // Render workflow runs
        pageRuns.forEach(run => {
            const row = document.createElement('tr');
            row.className = 'hover:bg-gray-50 cursor-pointer';

            {
                // Render database workflow run format (all users)
                const stateColors = {
                    'completed': 'bg-green-600 text-white',
                    'running': 'bg-sky-600 text-white',
                    'failed': 'bg-red-600 text-white',
                    'queued': 'bg-amber-500 text-white'
                };

                const stateColor = stateColors[run.status] || 'bg-gray-500 text-white';

                // Calculate duration
                let duration = '--';
                if (run.started_at && run.completed_at) {
                    const start = new Date(run.started_at);
                    const end = new Date(run.completed_at);
                    const diffMs = end - start;
                    const diffMins = Math.floor(diffMs / 60000);
                    const diffSecs = Math.floor((diffMs % 60000) / 1000);
                    duration = `${diffMins}m ${diffSecs}s`;
                } else if (run.started_at && run.status === 'running') {
                    const start = new Date(run.started_at);
                    const now = new Date();
                    const diffMs = now - start;
                    const diffMins = Math.floor(diffMs / 60000);
                    const diffSecs = Math.floor((diffMs % 60000) / 1000);
                    duration = `${diffMins}m ${diffSecs}s (running)`;
                }

                // BUG.B27 — the real workflow name (not the cryptic run_name),
                // the run number, who ran it, and a one-line outcome, so a
                // row is findable without opening Details.
                const wfLabel = run.workflow_name
                    ? `${run.workflow_name}${run.version_number ? ` v${run.version_number}` : ''}`
                    : `Workflow #${run.workflow_id}`;
                const actor = run.triggered_by || 'System';
                // BUG.B27b — a username alone cannot identify who ran a run
                // once the owner has hundreds of users (the same handle can
                // exist in several tenants), so the row also names the tenant
                // and carries the numeric Tenant/User ids.
                const tenantLabel = run.tenant_name
                    || (run.tenant_id != null ? `Tenant #${run.tenant_id}` : '');
                const userLabel = run.triggered_by_username
                    || (run.triggered_by_user_id != null ? `User #${run.triggered_by_user_id}` : actor);
                // BUG.B27b — "Ended" next to "Started": the completion time, or
                // an honest marker when the run has not finished.
                const endedAt = run.completed_at
                    ? formatTimestamp(run.completed_at)
                    : (run.status === 'running' ? 'running' : '—');
                const outcome = run.outcome ||
                    (run.node_runs && run.node_runs.length
                        ? `${run.node_runs.length} node(s)`
                        : '');

                row.innerHTML = `
                    <td class="py-3 px-4 text-sm text-gray-900">
                        <div class="font-medium">${escapeHtml(wfLabel)}</div>
                        <div class="text-xs text-gray-500">Run #${run.id} · ${escapeHtml(actor)}</div>
                    </td>
                    <td class="py-3 px-4 text-sm text-gray-600">
                        <div class="text-sm">${escapeHtml(tenantLabel)}</div>
                        <div class="text-xs text-gray-500">T${run.tenant_id != null ? run.tenant_id : ''}</div>
                    </td>
                    <td class="py-3 px-4 text-sm text-gray-600">
                        <div class="text-sm">${escapeHtml(userLabel)}</div>
                        <div class="text-xs text-gray-500">U${run.triggered_by_user_id != null ? run.triggered_by_user_id : ''}</div>
                    </td>
                    <td class="py-3 px-4">
                        <span class="px-2 py-1 text-xs font-medium rounded-full ${stateColor}">
                            ${run.status}
                        </span>
                    </td>
                    <td class="py-3 px-4 text-sm text-gray-600">${formatTimestamp(run.started_at)}</td>
                    <td class="py-3 px-4 text-sm text-gray-600">${endedAt}</td>
                    <td class="py-3 px-4 text-sm text-gray-600">${duration}</td>
                    <td class="py-3 px-4 text-sm text-gray-600">${escapeHtml(outcome)}</td>
                    <td class="py-3 px-4 text-right">
                        <button
                            onclick="window.logsPage.viewWorkflowRunDetails(${run.id})"
                            class="text-sm text-primary hover:text-primary/80 font-medium mr-2"
                        >
                            Details
                        </button>
                    </td>
                `;
            }

            tbody.appendChild(row);
        });

        // Update pagination info. No total-count query (the endpoint
        // returns a bare list other callers depend on), so report the
        // current window + whether more pages follow (limit+1 probe).
        const start = workflowPage * workflowPageSize + 1;
        const end = start + pageRuns.length - 1;
        const info = document.getElementById('workflow-results-info');
        if (info) info.textContent =
            `Showing ${start}-${end} (Page ${workflowPage + 1})`;

        const prev = document.getElementById('prev-workflow-page');
        if (prev) prev.disabled = workflowPage === 0;
        const next = document.getElementById('next-workflow-page');
        if (next) next.disabled = !hasNext;

    } catch (error) {
        console.error('Error loading workflow runs:', error);
        tbody.innerHTML = `
            <tr>
                <td colspan="9" class="py-4 text-center text-red-600">
                    Error loading workflow runs: ${escapeHtml(error.message)}
                </td>
            </tr>
        `;
    }
}

async function viewWorkflowDetails(nodeKind, dagRunId, workflowRunId) {
    try {
        // All runs use the DB-backed detail view (no external dependency)
        if (workflowRunId) {
            return await viewWorkflowRunDetails(workflowRunId);
        }

        const tasks = await apiCall(`/road/runs?limit=1`);

        const modal = document.getElementById('workflow-detail-modal');
        const content = document.getElementById('workflow-detail-content');

        // Check if workflow has failed tasks for AI analysis
        const hasFailed = tasks && tasks.some(t => t.state === 'failed');

        // Build details view
        let html = `
            <div class="mb-4 p-4 bg-gray-50 rounded-lg">
                <div class="flex items-center justify-between">
                    <div>
                        <h4 class="font-semibold text-gray-900 mb-2">Run Information</h4>
                        <div class="grid grid-cols-2 gap-2 text-sm">
                            <div><span class="font-medium">DAG ID:</span> ${escapeHtml(nodeKind)}</div>
                            <div><span class="font-medium">Run ID:</span> ${escapeHtml(dagRunId)}</div>
                        </div>
                    </div>
                    ${hasFailed ? `
                    <button
                        onclick="window.logsPage.analyzeWithAI('${nodeKind}', '${dagRunId}')"
                        class="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 transition-colors flex items-center gap-2"
                    >
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                        </svg>
                        AI Analysis
                    </button>
                    ` : ''}
                </div>
            </div>

            <div>
                <h4 class="font-semibold text-gray-900 mb-3">Task Instances</h4>
                <div class="space-y-2">
        `;

        if (tasks && tasks.length > 0) {
            tasks.forEach(task => {
                const stateColors = {
                    'success': 'bg-green-600 text-white',
                    'running': 'bg-sky-600 text-white',
                    'failed': 'bg-red-600 text-white',
                    'queued': 'bg-amber-500 text-white',
                    'upstream_failed': 'bg-orange-600 text-white'
                };

                const stateColor = stateColors[task.state] || 'bg-gray-500 text-white';

                html += `
                    <div class="flex items-center justify-between p-3 border border-gray-200 rounded-lg hover:bg-gray-50">
                        <div class="flex-1">
                            <p class="font-medium text-gray-900">${escapeHtml(task.task_id)}</p>
                            <p class="text-xs text-gray-500">Try ${task.try_number || 1}</p>
                        </div>
                        <div class="flex items-center gap-3">
                            <span class="px-2 py-1 text-xs font-medium rounded-full ${stateColor}">
                                ${task.state}
                            </span>
                            <button
                                onclick="window.logsPage.viewTaskLogs('${nodeKind}', '${dagRunId}', '${task.task_id}', ${task.try_number || 1})"
                                class="text-sm text-primary hover:text-primary/80"
                            >
                                View Logs →
                            </button>
                        </div>
                    </div>
                `;
            });
        } else {
            html += '<p class="text-center text-gray-500 py-4">No task instances found</p>';
        }

        html += `
                </div>
            </div>
        `;

        content.innerHTML = html;
        modal.classList.remove('hidden');

        // Initialize AI chat for failed workflows
        const workflowStatus = hasFailed ? 'failed' : 'success';
        await initAIChat(nodeKind, dagRunId, workflowStatus);

    } catch (error) {
        console.error('Error viewing workflow details:', error);
        showError('Failed to load workflow details: ' + error.message);
    }
}

async function viewWorkflowLogs(nodeKind, dagRunId) {
    // Deprecated: Log viewing updated. Use viewWorkflowRunDetails instead.
    console.warn('viewWorkflowLogs is deprecated — use viewWorkflowRunDetails');
    showInfo('Log viewing has been updated. Please use the Details button to view workflow logs.');
}

async function viewTaskLogs(nodeKind, dagRunId, taskId, tryNumber) {
    // Deprecated: use viewWorkflowRunDetails for log viewing
    console.warn('viewTaskLogs is deprecated');
    showLoadingOverlay('Loading logs...');

    try {
        // Container logs from worker task queue
        const data = { logs: 'Task logs are available in the workflow run detail view. Click Details to see node-by-node logs.' };
        const logs = data.logs || 'No logs available';

        // Hide loading overlay
        hideLoadingOverlay();

        // Store current logs for download
        currentTaskLogs = {
            nodeKind,
            dagRunId,
            taskId,
            tryNumber,
            content: logs
        };

        // Update modal content
        document.getElementById('log-dag-id').textContent = nodeKind;
        document.getElementById('log-task-id').textContent = taskId;
        document.getElementById('log-run-id').textContent = dagRunId;
        document.getElementById('log-try-number').textContent = tryNumber;
        document.getElementById('task-log-content').textContent = logs;

        // P5 — wire the logs-aware LLM panel using the new
        // /support/logs-chat endpoint. This replaces the older
        // workflow-AI-chat for the logs view; the floating support
        // chat in bottom-right still uses /support/chat.
        initLogsLLMPanel(taskId);

        // Show modal
        document.getElementById('task-logs-modal').classList.remove('hidden');

        // Auto-poll logs every 3 seconds while modal is open
        if (logPollInterval) clearInterval(logPollInterval);
        logPollInterval = setInterval(async () => {
            const modal = document.getElementById('task-logs-modal');
            if (!modal || modal.classList.contains('hidden')) {
                clearInterval(logPollInterval);
                logPollInterval = null;
                return;
            }
            try {
                const freshData = await apiCall(`/workers/tasks/${taskId}/container-logs`);
                const freshLogs = freshData.logs || '';
                if (freshLogs && freshLogs !== currentTaskLogs?.content) {
                    currentTaskLogs.content = freshLogs;
                    const logEl = document.getElementById('task-log-content');
                    if (logEl) {
                        const wasScrolledToBottom = logEl.parentElement.scrollHeight - logEl.parentElement.scrollTop <= logEl.parentElement.clientHeight + 50;
                        logEl.textContent = freshLogs;
                        if (wasScrolledToBottom) {
                            logEl.parentElement.scrollTop = logEl.parentElement.scrollHeight;
                        }
                    }
                }
            } catch (e) {
                // Silent — don't interrupt the user if a poll fails
            }
        }, 3000);

    } catch (error) {
        hideLoadingOverlay();
        console.error('Error showing task logs:', error);
        showError('Failed to show task logs: ' + error.message);
    }
}

function showLoadingOverlay(message = 'Loading...') {
    let overlay = document.getElementById('loading-overlay');
    if (!overlay) {
        // Create overlay if it doesn't exist
        overlay = document.createElement('div');
        overlay.id = 'loading-overlay';
        overlay.className = 'fixed inset-0 bg-gray-900 bg-opacity-50 flex items-center justify-center z-50';
        overlay.innerHTML = `
            <div class="bg-white rounded-lg p-6 shadow-xl">
                <div class="flex items-center space-x-3">
                    <svg class="animate-spin h-8 w-8 text-primary" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    <span id="loading-message" class="text-lg font-medium text-gray-800">${message}</span>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
    } else {
        // FIX-CE-NAV-REGROUP-CLASS-C — the folded Logs tab layout can leave an
        // overlay whose #loading-message span was moved/removed; writing through
        // a null element threw "Cannot set properties of null (setting
        // 'textContent')" and surfaced as "Error loading workflow runs".
        const msgEl = document.getElementById('loading-message');
        if (msgEl) msgEl.textContent = message;
        overlay.classList.remove('hidden');
    }
}

function hideLoadingOverlay() {
    const overlay = document.getElementById('loading-overlay');
    if (overlay) {
        overlay.classList.add('hidden');
    }
}

function closeTaskLogsModal() {
    if (logPollInterval) { clearInterval(logPollInterval); logPollInterval = null; }
    document.getElementById('task-logs-modal').classList.add('hidden');
    resetAIChat();
    resetLogsLLMPanel();
}

// Copy the full task-log text to the clipboard (Fix 26). Reads the rendered
// #task-log-content element (the live source of truth — the poll writes there),
// falling back to the stored currentTaskLogs.content. Uses the async Clipboard
// API with a legacy execCommand fallback for insecure-origin / older browsers,
// and briefly flips the button label to "Copied" as confirmation.
async function copyTaskLogs() {
    const el = document.getElementById('task-log-content');
    const text = (el && el.textContent) || (currentTaskLogs && currentTaskLogs.content) || '';
    if (!text.trim()) {
        showInfo('No logs to copy');
        return;
    }
    const btn = document.getElementById('copy-task-logs-btn');
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
        } else {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        }
        if (btn) {
            const original = btn.textContent;
            btn.textContent = 'Copied';
            setTimeout(() => { btn.textContent = original; }, 1500);
        }
    } catch (e) {
        showError('Could not copy logs to clipboard');
    }
}

// Fix A — copy the current Workflow Run Details log to the clipboard. Uses the
// plaintext run summary captured when the modal was populated (status, per-node
// PASS/FAIL, errors, orchestrator events). Async Clipboard API with an
// execCommand fallback for insecure-origin / older browsers; briefly flips the
// button label to "Copied" as confirmation.
async function copyRunDetailLog() {
    const text = currentRunDetailLog || '';
    if (!text.trim()) {
        showInfo('No logs to copy');
        return;
    }
    const btn = document.getElementById('copy-workflow-detail-log-btn');
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
        } else {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        }
        if (btn) {
            const original = btn.textContent;
            btn.textContent = 'Copied';
            setTimeout(() => { btn.textContent = original; }, 1500);
        }
    } catch (e) {
        showError('Could not copy logs to clipboard');
    }
}

// Generic raw-log copy helper (shared). Copies the textContent of a raw-log
// element to the clipboard so every stdout/stderr/container-log surface — not
// just the two modal-level buttons — has its own copy control. Resolves the
// source element either by id (`sourceElId`) or by finding the <pre>/<code>
// inside the button's enclosing [data-log-block]. Uses the async Clipboard API
// with a legacy execCommand fallback for insecure-origin / older browsers, and
// briefly flips the trigger button's label to "Copied" as confirmation.
async function copyRawLog(btn, sourceElId) {
    let el = null;
    if (sourceElId) {
        el = document.getElementById(sourceElId);
    } else if (btn) {
        const block = btn.closest('[data-log-block]');
        el = block ? block.querySelector('pre, code') : (btn.parentElement && btn.parentElement.querySelector('pre, code'));
    }
    const text = (el && el.textContent) || '';
    if (!text.trim() || text.trim() === 'Loading logs...') {
        showInfo('No logs to copy');
        return;
    }
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
        } else {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        }
        if (btn) {
            const original = btn.textContent;
            btn.textContent = 'Copied';
            setTimeout(() => { btn.textContent = original; }, 1500);
        }
    } catch (e) {
        showError('Could not copy logs to clipboard');
    }
}
window.copyRawLog = copyRawLog;

function downloadTaskLogs() {
    if (!currentTaskLogs) {
        showInfo('No logs to download');
        return;
    }

    const filename = `${currentTaskLogs.nodeKind}_${currentTaskLogs.taskId}_${currentTaskLogs.tryNumber}.log`;
    const blob = new Blob([currentTaskLogs.content], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);

    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

// BUG.B29b2 — the Permission Audit tab is gone from this page (its loader,
// filters, pagination and the #13 CSV/JSON export went with it). The audit log
// now lives in the Compliance hub's "Audit log" tab, which owns its own search
// and export (js/audit-logs.js). Old #logs?tab=permissions bookmarks are
// redirected there by ui.js; anything landing here falls back to Workflow Runs.

// ========================================
// Utility Functions
// ========================================

/**
 * Show a toast notification
 * @param {string} message - The message to display
 * @param {string} type - 'success', 'error', 'info', 'warning'
 * @param {number} duration - Duration in ms (0 for persistent)
 */
function showToast(message, type = 'info', duration = 3000) {
    // Remove existing toast if any
    const existingToast = document.getElementById('toast-notification');
    if (existingToast) {
        existingToast.remove();
    }

    const colors = {
        success: 'bg-green-500',
        error: 'bg-red-500',
        info: 'bg-primary/80',
        warning: 'bg-yellow-500'
    };

    const toast = document.createElement('div');
    toast.id = 'toast-notification';
    toast.className = `fixed bottom-4 right-4 ${colors[type] || colors.info} text-white px-6 py-3 rounded-lg shadow-lg z-50 transition-opacity duration-300`;
    toast.textContent = message;

    document.body.appendChild(toast);

    if (duration > 0) {
        setTimeout(() => {
            toast.style.opacity = '0';
            setTimeout(() => toast.remove(), 300);
        }, duration);
    }
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formatTimestamp(timestamp) {
    if (!timestamp) return '-';
    const date = new Date(timestamp);
    return date.toLocaleString();
}

function formatAction(action) {
    const actionMap = {
        'permission_granted': { label: 'Granted', color: 'green' },
        'permission_revoked': { label: 'Revoked', color: 'red' },
        'permission_updated': { label: 'Updated', color: 'yellow' },
        'permission_requested': { label: 'Requested', color: 'blue' },
        'permission_request_approved': { label: 'Approved', color: 'green' },
        'permission_request_denied': { label: 'Denied', color: 'red' },
        'permission_request_cancelled': { label: 'Cancelled', color: 'gray' },
    };

    const info = actionMap[action] || { label: action, color: 'gray' };
    return `<span class="px-2 py-1 rounded-full text-xs font-medium bg-${info.color}-100 text-${info.color}-800">${info.label}</span>`;
}

/**
 * Analyze workflow logs with AI
 * Shows loading state and displays AI analysis in a modal
 */
async function analyzeWithAI(nodeKind, dagRunId) {
    try {
        // Show loading toast
        showToast('Starting AI analysis... (may take 30s if AI is sleeping)', 'info', 0);

        // Call AI analysis endpoint
        const result = await apiCall(`/assistant/analyze-dag/${nodeKind}/${dagRunId}`, {
            method: 'POST'
        });

        // Close loading toast
        showToast('AI analysis complete!', 'success', 3000);

        // Show AI analysis in modal
        const modal = document.getElementById('workflow-detail-modal');
        const content = document.getElementById('workflow-detail-content');

        const html = `
            <div class="mb-4 p-4 bg-purple-50 border border-purple-200 rounded-lg">
                <div class="flex items-center gap-2 mb-2">
                    <svg class="w-6 h-6 text-purple-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                    </svg>
                    <h4 class="font-semibold text-purple-900">AI Analysis</h4>
                    ${result.startup_time_seconds > 0 ?
                        `<span class="text-xs text-purple-600">(AI started in ${result.startup_time_seconds.toFixed(1)}s)</span>`
                        : ''}
                </div>
                <div class="prose prose-sm max-w-none text-gray-800 whitespace-pre-wrap">${escapeHtml(result.analysis)}</div>
            </div>

            <div class="mt-4">
                <button
                    onclick="window.logsPage.viewWorkflowDetails('${nodeKind}', '${dagRunId}')"
                    class="text-sm text-primary hover:text-primary/80"
                >
                    ← Back to Task Details
                </button>
            </div>
        `;

        content.innerHTML = html;
        modal.classList.remove('hidden');

    } catch (error) {
        console.error('Error analyzing with AI:', error);

        if (error.status === 404) {
            showToast('AI Assistant is not enabled on this system', 'error', 5000);
        } else if (error.status === 503) {
            showToast('AI Assistant is currently unavailable. Please try again later.', 'error', 5000);
        } else {
            showToast(`AI analysis failed: ${error.message}`, 'error', 5000);
        }
    }
}

// ========================================
// Workflow Run Details (using our API)
// ========================================

async function viewWorkflowRunDetails(runId) {
    try {
        const data = await apiCall(`/road/workflow-runs/${runId}/detailed-log`);

        const modal = document.getElementById('workflow-detail-modal');
        const content = document.getElementById('workflow-detail-content');

        const statusIcon = data.status === 'completed' ? '&#10004;' : data.status === 'failed' ? '&#10008;' : '&#9203;';
        const statusClass = getStatusColor(data.status);

        // Run-level tally: pass / fail / skipped counts come from the
        // API; we surface them in the header so operators see at a
        // glance whether the run is green, red, or mixed.
        const passCount    = data.pass_count    || 0;
        const failCount    = data.fail_count    || 0;
        const skippedCount = data.skipped_count || 0;
        const totalNodes   = data.total_nodes   || (data.nodes || []).length;

        let html = `
            <!-- Workflow Summary -->
            <div class="mb-4 p-4 bg-gray-50 rounded-lg">
                <div class="flex items-center justify-between flex-wrap gap-3">
                    <div>
                        <h4 class="font-semibold text-gray-900 text-lg">${escapeHtml(data.workflow_name || 'Workflow')} v${data.version} Run #${data.workflow_run_id}</h4>
                        <div class="flex items-center gap-4 mt-2 text-sm text-gray-600 flex-wrap">
                            <span class="px-2 py-1 text-xs font-medium rounded-full ${statusClass}">${data.status}</span>
                            ${data.total_duration_s != null ? `<span>Total: <strong>${data.total_duration_s}s</strong></span>` : ''}
                            ${data.started_at  ? `<span>Started: ${formatTimestamp(data.started_at)}</span>`  : ''}
                            ${data.completed_at ? `<span>Finished: ${formatTimestamp(data.completed_at)}</span>` : ''}
                        </div>
                    </div>
                    <div class="flex items-center gap-2 text-xs">
                        <span class="px-2 py-1 rounded-full bg-green-600 text-white font-medium">PASS ${passCount}</span>
                        <span class="px-2 py-1 rounded-full bg-red-600 text-white font-medium">FAIL ${failCount}</span>
                        ${skippedCount > 0 ? `<span class="px-2 py-1 rounded-full bg-gray-500 text-white font-medium">SKIPPED ${skippedCount}</span>` : ''}
                        <span class="px-2 py-1 rounded-full bg-slate-100 text-slate-700 font-medium">${totalNodes} nodes</span>
                    </div>
                </div>
            </div>

        `;

        // Inline progress bar — fetch progress data and render at top
        try {
            const progress = await apiCall(`/road/workflow-runs/${runId}/progress`);
            const pct = Math.round(progress.progress_percentage || 0);
            const completed = progress.completed_nodes || 0;
            const total = progress.total_nodes || 0;
            const cost = progress.total_cost || 0;
            const eta = progress.estimated_seconds_remaining;
            const etaDisplay = eta ? `~${Math.ceil(eta / 60)}m` : (pct >= 100 ? 'Done' : '—');
            const barColor = pct >= 100 ? 'bg-green-500' : data.status === 'failed' ? 'bg-red-500' : 'bg-primary/80';

            html += `
            <div class="mb-4 px-4 py-3 bg-white border rounded-lg">
                <div class="flex justify-between text-xs text-gray-600 mb-1">
                    <span>${completed} / ${total} nodes complete</span>
                    <span class="flex gap-3">
                        <span class="font-mono font-bold">${pct}%</span>
                        <span>${etaDisplay}</span>
                    </span>
                </div>
                <div class="w-full bg-gray-200 rounded-full h-2.5 overflow-hidden">
                    <div class="${barColor} h-full rounded-full transition-all duration-700" style="width: ${pct}%"></div>
                </div>
            </div>`;

            // Orchestrator failure reason — surfaces the REAL cause
            // when a run fails before any node actually started
            // (pending_scan blocker, missing secret, circular dep).
            // Without this panel the Workflow Run Details view just
            // shows every node "queued" with no explanation.
            const errMsg = progress.error_message;
            if (errMsg && String(data.status).toLowerCase() === 'failed') {
                html += `
                <div class="mb-4 p-4 bg-red-600 rounded-lg">
                    <div class="flex items-center gap-2 font-semibold text-white mb-1">
                        <span>&#9888;</span>
                        <span>Failure reason</span>
                    </div>
                    <div class="text-sm text-white font-mono whitespace-pre-wrap break-words">${escapeHtml(errMsg)}</div>
                </div>`;
            }
        } catch (e) { /* progress endpoint failed — skip, details still render */ }

        // #12 (launch-day-followup-2) — orchestrator / traffic-cop
        // event strip. Surfaces what the system was doing between
        // container completions ("dispatched task X", "no eligible
        // worker for queue Y", …) so the user isn't staring at a
        // gap in the timeline. Hidden when no events were emitted
        // (older runs predate the event log).
        // #12 (round 6) — interleave orchestrator/traffic-cop/worker
        // events into the existing node pipeline below instead of
        // rendering a separate strip. Bucket events by their target:
        //   • workflowEvents — events without a node_id (workflow
        //     start / completion).
        //   • nodeEvents[node_id] — events about a specific node
        //     (worker pickup, task COMPLETED/FAILED, dispatch failure).
        //   • connectorEvents[sourceNodeId] — downstream-dispatch
        //     decisions ("Enqueued downstream node X"); the source
        //     is the completed node, the connector renders between
        //     source and the dispatched downstream node.
        //   • blockedEvents — failed-cascade rows; rendered with the
        //     failed node's expanded panel.
        const runEvents = data.run_events || [];
        const workflowEvents = [];
        const nodeEventsByNode = {};
        const connectorEventsByNode = {};
        for (const ev of runEvents) {
            const md = ev.metadata || ev.event_metadata || {};
            // Downstream-dispatch event: tie to the COMPLETED node (the
            // source) so it draws a connector to the next node. Source
            // node is the event's owning task — but the API emits these
            // from the completed_task's worker_run_id without a
            // node_id; we infer source from the completed_node tracked
            // in the message. Cleaner: API ships `from_node_id` in
            // metadata too. For now, fall back to scanning the message.
            if (md.downstream_node_id) {
                // Source node = the completed task that triggered the
                // dispatch. Walk back through prior events to find the
                // most recent traffic_cop COMPLETED for *some* node and
                // attribute the connector to it. Cheap heuristic; the
                // API can ship a `from_node_id` field next round.
                let fromNodeId = md.from_node_id;
                if (!fromNodeId) {
                    for (let i = runEvents.indexOf(ev) - 1; i >= 0; i--) {
                        const prev = runEvents[i];
                        const pmd = prev.metadata || prev.event_metadata || {};
                        if (prev.source === 'traffic_cop' && pmd.node_id && /COMPLETED|FAILED/.test(prev.message || '')) {
                            fromNodeId = pmd.node_id;
                            break;
                        }
                    }
                }
                if (fromNodeId) {
                    if (!connectorEventsByNode[fromNodeId]) connectorEventsByNode[fromNodeId] = [];
                    connectorEventsByNode[fromNodeId].push(ev);
                    continue;
                }
            }
            if (md.node_id) {
                if (!nodeEventsByNode[md.node_id]) nodeEventsByNode[md.node_id] = [];
                nodeEventsByNode[md.node_id].push(ev);
                continue;
            }
            // No node binding → a workflow-level event.
            workflowEvents.push(ev);
        }

        // Helpers shared by the workflow-level header and the per-node
        // event sections below.
        const sourceLabel = (s) => s === 'orchestrator' ? '⚙' : s === 'traffic_cop' ? '🚦' : s === 'worker' ? '🔧' : '·';
        const levelClass = (lvl) => lvl === 'error' ? 'text-red-700 bg-red-50 border-red-200'
            : (lvl === 'warn' || lvl === 'warning') ? 'text-amber-700 bg-amber-50 border-amber-200'
            : 'text-gray-700 bg-gray-50 border-gray-200';
        const renderEventMeta = (md) => {
            if (!md || typeof md !== 'object') return '';
            const lines = [];
            if (md.task_id != null) lines.push(`task=${md.task_id}`);
            if (md.queue) lines.push(`queue=${md.queue}`);
            if (md.worker) lines.push(`worker=${md.worker}`);
            if (md.duration_seconds != null) lines.push(`duration=${md.duration_seconds}s`);
            if (md.attempt != null) lines.push(`attempt=${md.attempt}`);
            if (md.node_kind) lines.push(`dag=${md.node_kind}`);
            const tag = lines.length > 0
                ? `<div class="font-mono text-[10px] text-gray-500 mt-0.5">${escapeHtml(lines.join(' · '))}</div>`
                : '';
            const err = md.error_message
                ? `<details class="mt-1" data-log-block><summary class="cursor-pointer text-[11px] text-red-700">error_message</summary><div class="flex justify-end"><button onclick="copyRawLog(this)" class="text-[10px] px-2 py-0.5 mt-1 bg-gray-200 text-gray-700 rounded hover:bg-gray-300">Copy</button></div><pre class="text-[11px] mt-1 whitespace-pre-wrap break-words bg-red-50 border border-red-200 p-2 rounded">${escapeHtml(md.error_message)}</pre></details>`
                : '';
            const log = md.log_tail
                ? `<details class="mt-1" data-log-block><summary class="cursor-pointer text-[11px] text-gray-700">container log tail</summary><div class="flex justify-end"><button onclick="copyRawLog(this)" class="text-[10px] px-2 py-0.5 mt-1 bg-gray-200 text-gray-700 rounded hover:bg-gray-300">Copy</button></div><pre class="text-[11px] mt-1 whitespace-pre-wrap break-words bg-gray-900 text-gray-100 p-2 rounded max-h-64 overflow-auto">${escapeHtml(md.log_tail)}</pre></details>`
                : '';
            return `${tag}${err}${log}`;
        };
        const renderEventLi = (ev) => {
            const md = ev.metadata || ev.event_metadata || {};
            return `
                <li class="text-xs px-2 py-1 rounded border ${levelClass(ev.level)}">
                    <div class="flex items-start gap-2">
                        <span class="font-mono text-gray-500 shrink-0">${ev.ts ? new Date(ev.ts).toLocaleTimeString() : '—'}</span>
                        <span class="shrink-0" title="${escapeHtml(ev.source || '')}">${sourceLabel(ev.source)}</span>
                        <div class="flex-1 min-w-0">
                            <div class="font-medium break-words">${escapeHtml(ev.message || '')}</div>
                            ${renderEventMeta(md)}
                        </div>
                    </div>
                </li>
            `;
        };

        // Header — the "started" workflow event (and any other early
        // workflow-level events that aren't completion). Compact
        // single-line list above the pipeline so the timeline still
        // dominates the visual.
        const startedEvents = workflowEvents.filter(ev => !/finished/i.test(ev.message || ''));
        if (startedEvents.length > 0) {
            html += `
                <ol class="space-y-1 mb-3">
                    ${startedEvents.map(renderEventLi).join('')}
                </ol>
            `;
        }

        html += `
            <!-- Node Execution Pipeline -->
            <div class="space-y-1">
        `;

        // Build wait_time lookup
        const waitMap = {};
        (data.wait_times || []).forEach(w => {
            waitMap[w.between[0] + '->' + w.between[1]] = w.wait_s;
        });

        const nodes = data.nodes || [];
        for (let i = 0; i < nodes.length; i++) {
            const node = nodes[i];
            const isWorkerTask = node.type === 'worker_task';
            // Resolve a derived PASS / FAIL / SKIPPED / RUNNING label
            // matching the run-level tally rules (system_task_status
            // capability) so terminal rows always read pass or fail.
            const wt = node.worker_task || {};
            const errMsg = wt.error_message || '';
            let derivedLabel = node.status;
            let derivedClass = getStatusColor(node.status);
            if (node.status === 'completed' && !errMsg) {
                derivedLabel = 'PASS'; derivedClass = 'bg-green-600 text-white';
            } else if (
                node.status === 'failed' || node.status === 'expired' || node.status === 'cancelled'
                || (node.status === 'completed' && errMsg)
            ) {
                derivedLabel = 'FAIL'; derivedClass = 'bg-red-600 text-white';
            } else if (node.status === 'skipped') {
                derivedLabel = 'SKIPPED'; derivedClass = 'bg-gray-500 text-white';
            } else if (['queued', 'running', 'pending', 'claimed'].includes(node.status)) {
                derivedClass = 'bg-sky-600 text-white';
            }

            const isFailed = derivedLabel === 'FAIL';
            const rowOuterClass = isFailed
                ? 'border border-red-300 rounded-lg overflow-hidden'
                : 'border rounded-lg overflow-hidden';
            const rowInnerClass = isFailed
                ? 'flex items-center justify-between px-4 py-3 bg-red-50 hover:bg-red-100 cursor-pointer'
                : 'flex items-center justify-between px-4 py-3 bg-white hover:bg-gray-50 cursor-pointer';
            const nodeId = `node-detail-${node.node_id}`;

            // Node row — failed nodes get a visible red treatment so
            // operators can spot them without expanding every row.
            html += `
                <div class="${rowOuterClass}">
                    <div class="${rowInnerClass}"
                         onclick="document.getElementById('${nodeId}').classList.toggle('hidden'); this.querySelector('.rotate-arrow').classList.toggle('rotate-90'); this.setAttribute('aria-expanded', this.getAttribute('aria-expanded')==='true'?'false':'true')">
                        <div class="flex items-center gap-3">
                            <span class="inline-block text-gray-400 text-xs rotate-arrow transition-transform">&#9654;</span>
                            <span class="font-medium text-sm">${escapeHtml(node.node_id)}</span>
                            <span class="text-gray-500 text-sm">${escapeHtml(node.name)}</span>
                        </div>
                        <div class="flex items-center gap-3">
                            <span class="px-2 py-0.5 text-xs rounded-full ${derivedClass} font-semibold">${escapeHtml(derivedLabel)}</span>
                            ${node.duration_s != null ? `<span class="text-sm font-mono text-gray-600">${node.duration_s}s</span>` : ''}
                            <span class="text-xs text-gray-400">${isWorkerTask ? 'Worker Task' : 'Passthrough'}</span>
                        </div>
                    </div>
            `;

            // Expandable detail
            html += `<div id="${nodeId}" class="hidden border-t bg-gray-50 px-4 py-3">`;

            if (isWorkerTask && node.worker_task) {
                const wt = node.worker_task;
                const phases = wt.phases || wt.phase_durations || {};

                // Error message + stderr tail surface up top for FAIL —
                // the most common reason an operator opens this view.
                if (isFailed) {
                    if (wt.error_message) {
                        html += `
                            <div class="mb-3 p-3 bg-red-600 rounded-md">
                                <div class="text-xs font-semibold text-white mb-1">Error</div>
                                <div class="text-xs font-mono text-white whitespace-pre-wrap break-words">${escapeHtml(wt.error_message)}</div>
                            </div>
                        `;
                    }
                    if (wt.stderr_tail) {
                        html += `
                            <div class="mb-3" data-log-block>
                                <div class="flex items-center justify-between mb-1">
                                    <div class="text-xs font-semibold text-gray-700">stderr (tail)</div>
                                    <button onclick="copyRawLog(this)" class="text-xs px-2 py-0.5 bg-gray-200 text-gray-700 rounded hover:bg-gray-300">Copy</button>
                                </div>
                                <pre class="p-2 bg-gray-900 text-red-300 text-xs rounded max-h-40 overflow-y-auto font-mono whitespace-pre-wrap">${escapeHtml(wt.stderr_tail)}</pre>
                            </div>
                        `;
                    }
                }

                // Worker / container info
                html += `
                    <div class="grid grid-cols-2 gap-2 text-xs text-gray-600 mb-3">
                        <div><span class="font-medium">Worker:</span> ${escapeHtml(wt.worker_name || '—')}</div>
                        <div><span class="font-medium">Task ID:</span> ${wt.task_id}</div>
                        <div class="col-span-2"><span class="font-medium">Image:</span> <span class="font-mono">${escapeHtml(wt.processing_image || '—')}</span></div>
                    </div>
                `;

                // I/O — input + output hashes as clickable chips that
                // route to the data page filtered by content hash.
                const inHashes  = wt.input_hashes  || (wt.input_hash ? [wt.input_hash] : []);
                const outHashes = wt.output_hashes || [];
                const renderHashChip = (h, kind) => {
                    if (!h) return '';
                    const short = String(h).length > 20 ? String(h).slice(0, 20) + '…' : String(h);
                    const tooltip = `${kind} hash: ${h}\\nClick to view in Data`;
                    return `<a href="#files?hash=${encodeURIComponent(h)}" title="${escapeHtml(tooltip)}" class="inline-block font-mono text-[11px] px-2 py-0.5 mr-1 mb-1 rounded bg-slate-100 hover:bg-slate-200 text-slate-700 border border-slate-200">${escapeHtml(short)}</a>`;
                };
                if (inHashes.length || outHashes.length) {
                    html += `
                        <div class="mb-3 grid grid-cols-2 gap-3">
                            <div>
                                <div class="text-xs font-medium text-gray-500 mb-1">Inputs</div>
                                <div class="flex flex-wrap">${inHashes.length ? inHashes.map(h => renderHashChip(h, 'Input')).join('') : '<span class="text-xs text-gray-400">—</span>'}</div>
                            </div>
                            <div>
                                <div class="text-xs font-medium text-gray-500 mb-1">Outputs</div>
                                <div class="flex flex-wrap">${outHashes.length ? outHashes.map(h => renderHashChip(h, 'Output')).join('') : '<span class="text-xs text-gray-400">—</span>'}</div>
                            </div>
                        </div>
                    `;
                }

                // Phase timeline bar
                const phaseEntries = Object.entries(phases).filter(([_, v]) => v > 0);
                if (phaseEntries.length > 0) {
                    const totalPhase = phaseEntries.reduce((sum, [_, v]) => sum + v, 0);
                    const colors = {
                        queue_s: 'bg-yellow-400', download_s: 'bg-primary/60', pull_s: 'bg-purple-400',
                        process_s: 'bg-green-500', upload_s: 'bg-primary/60', cleanup_s: 'bg-gray-400'
                    };
                    const labels = {
                        queue_s: 'Queue', download_s: 'Download', pull_s: 'Pull',
                        process_s: 'Process', upload_s: 'Upload', cleanup_s: 'Cleanup'
                    };

                    html += `<div class="mb-3">
                        <div class="text-xs font-medium text-gray-500 mb-1">Phase Timeline</div>
                        <div class="flex h-6 rounded overflow-hidden">`;
                    for (const [phase, duration] of phaseEntries) {
                        const pct = Math.max((duration / totalPhase) * 100, 2);
                        const color = colors[phase] || 'bg-gray-300';
                        const label = labels[phase] || phase;
                        html += `<div class="${color} relative group" style="width:${pct}%" title="${label}: ${duration}s">
                            <span class="absolute inset-0 flex items-center justify-center text-[10px] text-white font-medium truncate px-1">
                                ${pct > 10 ? `${label} ${duration}s` : ''}
                            </span>
                        </div>`;
                    }
                    html += `</div>
                        <div class="flex gap-3 mt-1 text-[10px] text-gray-500">`;
                    for (const [phase, duration] of phaseEntries) {
                        const color = colors[phase] || 'bg-gray-300';
                        const label = labels[phase] || phase;
                        html += `<span class="flex items-center gap-1"><span class="w-2 h-2 rounded-sm ${color}"></span>${label}: ${duration}s</span>`;
                    }
                    html += `</div></div>`;
                }

                // Container logs button + inline display
                html += `
                    <div>
                        <button onclick="loadContainerLogs(${wt.task_id}, '${nodeId}-logs')"
                                class="text-xs px-3 py-1.5 bg-gray-800 text-green-400 rounded hover:bg-gray-700 mb-2">
                            View Container Logs
                        </button>
                        <button onclick="copyRawLog(this, '${nodeId}-logs')"
                                class="text-xs px-3 py-1.5 bg-gray-200 text-gray-700 rounded hover:bg-gray-300 mb-2 ml-2">
                            Copy
                        </button>
                        <pre id="${nodeId}-logs" class="hidden mt-2 p-3 bg-gray-900 text-green-400 text-xs rounded max-h-64 overflow-y-auto font-mono whitespace-pre-wrap"></pre>
                    </div>
                `;
            } else {
                // Passthrough — still surface enough that operators can
                // see *that* it ran and *what data* moved through it.
                // Never just "forwards data without processing" alone.
                const passthroughInputHashes = (node.worker_task && node.worker_task.input_hashes) || [];
                html += `
                    <div class="text-xs text-gray-600 space-y-2">
                        <div><span class="font-medium">Status:</span> ${escapeHtml(derivedLabel)}</div>
                        ${node.duration_s != null ? `<div><span class="font-medium">Duration:</span> ${node.duration_s}s</div>` : ''}
                        ${node.started_at  ? `<div><span class="font-medium">Started:</span> ${escapeHtml(node.started_at)}</div>`  : ''}
                        ${node.completed_at ? `<div><span class="font-medium">Finished:</span> ${escapeHtml(node.completed_at)}</div>` : ''}
                        ${passthroughInputHashes.length ? `<div><span class="font-medium">Input:</span> <span class="font-mono">${escapeHtml(passthroughInputHashes[0])}</span></div>` : ''}
                        <div class="text-gray-500 pt-1 border-t border-gray-200">Passthrough node — forwards data without processing.</div>
                    </div>
                `;
            }

            // #12 — node-scoped orchestrator events (worker pickup,
            // task COMPLETED/FAILED with log_tail, dispatch errors).
            // Renders inside the same expanded detail block so the
            // user sees what the system did for THIS node alongside
            // the node's own metadata.
            const thisNodeEvents = nodeEventsByNode[node.node_id] || [];
            if (thisNodeEvents.length > 0) {
                html += `
                    <div class="mt-3 pt-3 border-t border-gray-200">
                        <div class="text-xs font-semibold text-gray-700 mb-2">Orchestrator activity for this node</div>
                        <ol class="space-y-1">
                            ${thisNodeEvents.map(renderEventLi).join('')}
                        </ol>
                    </div>
                `;
            }

            html += `</div></div>`;

            // Wait time + cross-node connector events between this and
            // the next node. Downstream-dispatch events ("Enqueued
            // downstream node X (task Y, queue=Z)") render here as a
            // thin between-rows line, matching the visual style of
            // the existing wait_s connector. This is what Tim asked
            // for: keep the pretty pipeline as the spine, but fold
            // the new info inline.
            if (i < nodes.length - 1) {
                const waitKey = node.node_id + '->' + nodes[i + 1].node_id;
                const waitS = waitMap[waitKey];
                if (waitS != null && waitS > 0) {
                    html += `<div class="text-center text-[10px] text-gray-400 py-0.5">&#9203; Wait: ${waitS}s</div>`;
                }
                const conn = connectorEventsByNode[node.node_id] || [];
                if (conn.length > 0) {
                    html += `
                        <div class="px-6 py-1">
                            <ol class="space-y-1">
                                ${conn.map(renderEventLi).join('')}
                            </ol>
                        </div>
                    `;
                }
            } else {
                // Last node — still render any dangling connector events
                // (fan-out off the final node, e.g. completion-trigger
                // dispatch).
                const conn = connectorEventsByNode[node.node_id] || [];
                if (conn.length > 0) {
                    html += `
                        <div class="px-6 py-1">
                            <ol class="space-y-1">
                                ${conn.map(renderEventLi).join('')}
                            </ol>
                        </div>
                    `;
                }
            }
        }

        html += `</div>`;

        // Footer — workflow-completion event (final status, duration).
        const finishedEvents = workflowEvents.filter(ev => /finished/i.test(ev.message || ''));
        if (finishedEvents.length > 0) {
            html += `
                <ol class="space-y-1 mt-3">
                    ${finishedEvents.map(renderEventLi).join('')}
                </ol>
            `;
        }

        content.innerHTML = html;
        modal.classList.remove('hidden');

        // P5 — wire the workflow-run LLM panel using /support/logs-chat.
        // taskId namespace: "wfrun:<id>" so the support_chat backend
        // doesn't try to look it up as an ApiTaskQueue row, while still
        // pinning a stable session per run.
        // Per-node line: include the actual error_message + log tail for
        // failed nodes so the assistant can cite a real cause instead of
        // guessing from a bare "status=failed".
        const fmtNode = n => {
            let s = `- ${n.node_id} (${n.name}) status=${n.status}${n.duration_s != null ? ` duration_s=${n.duration_s}` : ''}`;
            const wt = n.worker_task || {};
            if (wt.error_message) s += `\n    error: ${String(wt.error_message).slice(0, 400)}`;
            const tail = wt.stderr_tail || wt.log_tail || '';
            if (n.status === 'failed' && tail) s += `\n    log_tail: ${String(tail).slice(-600)}`;
            return s;
        };
        // Orchestrator event log — the traffic-cop's account of the run.
        // Essential when a run fails BEFORE any node executes (e.g. no
        // eligible worker / scheduling error): the cause lives here, not
        // in node output. This is exactly the gap that made the helper
        // hallucinate on "failed but all nodes queued" runs.
        const events = (data.run_events || [])
            .filter(e => e && e.message)
            .slice(-20)
            .map(e => `  [${e.level || 'info'}] ${e.source || ''}: ${e.message}`);
        const runSummary = [
            `WORKFLOW: ${data.workflow_name || 'unknown'} v${data.version || '?'} (run #${data.workflow_run_id || runId})`,
            `STATUS: ${data.status || 'unknown'}`,
            data.total_duration_s != null ? `DURATION_S: ${data.total_duration_s}` : '',
            data.started_at ? `STARTED: ${data.started_at}` : '',
            `TALLY: pass=${data.pass_count ?? '?'} fail=${data.fail_count ?? '?'} skipped=${data.skipped_count ?? '?'}`,
            '',
            'NODES:',
            ...(data.nodes || []).map(fmtNode),
            events.length ? '\nORCHESTRATOR EVENTS:\n' + events.join('\n') : '',
        ].filter(Boolean).join('\n');

        // Fix A — stash the plaintext run summary so the modal's "Copy Logs"
        // button copies the real run log (status, per-node PASS/FAIL, errors,
        // orchestrator events) rather than nothing.
        currentRunDetailLog = runSummary;

        initLogsLLMPanel(`wfrun:${runId}`, {
            panelSelector: '#workflow-llm-panel',
            messagesSelector: '#workflow-llm-messages',
            formSelector: '#workflow-llm-form',
            inputSelector: '#workflow-llm-input',
            placeholderHTML:
                '<p class="text-xs"><strong>Ask me about this workflow run.</strong></p>' +
                '<p class="text-xs text-gray-400 mt-1">' +
                "E.g., \"Why did this fail?\" or \"Which node took longest?\"</p>",
            logTailGetter: () => runSummary,
        });

    } catch (error) {
        console.error('Error loading workflow run details:', error);
        showToast(`Error loading details: ${error.message}`, 'error', 5000);
    }
}

async function loadContainerLogs(taskId, targetElementId) {
    const el = document.getElementById(targetElementId);
    if (!el) return;
    el.classList.remove('hidden');
    el.textContent = 'Loading logs...';
    try {
        const data = await apiCall(`/workers/tasks/${taskId}/container-logs`);
        if (data.lines && data.lines.length > 0) {
            el.textContent = data.lines.join('\n');
        } else {
            // LDF-3 §6 — without the "container startup can take several
            // minutes" caveat, users thought slow image-pull / GPU warm-up
            // was a hung run.
            el.textContent =
                "Waiting for the first log line — the worker streams output to Redis once the container starts writing.\n" +
                "Container startup (image pull, GPU warm-up) can take several minutes before the first log line appears. This is normal.";
        }
    } catch (err) {
        el.textContent = `Error loading logs: ${err.message}`;
    }
}

async function viewWorkflowRunProgress(runId) {
    try {
        const progress = await apiCall(`/road/workflow-runs/${runId}/progress`);

        const modal = document.getElementById('workflow-detail-modal');
        const content = document.getElementById('workflow-detail-content');

        const pct = Math.round(progress.progress_percentage || 0);
        const completed = progress.completed_nodes || 0;
        const total = progress.total_nodes || 0;
        const status = progress.status?.value || progress.status || 'unknown';
        const currentNode = progress.current_node || '—';
        const cost = progress.total_cost || 0;
        const eta = progress.estimated_seconds_remaining;
        const etaDisplay = eta ? `~${Math.ceil(eta / 60)} min remaining` : (pct >= 100 ? 'Done' : 'Calculating...');

        const barColor = pct >= 100 ? 'bg-green-500' : status === 'failed' ? 'bg-red-500' : 'bg-primary/80';
        const statusBadge = {
            'completed': '<span class="px-2 py-1 rounded text-xs font-semibold bg-green-600 text-white">Completed</span>',
            'running':   '<span class="px-2 py-1 rounded text-xs font-semibold bg-sky-600 text-white">Running</span>',
            'failed':    '<span class="px-2 py-1 rounded text-xs font-semibold bg-red-600 text-white">Failed</span>',
            'pending':   '<span class="px-2 py-1 rounded text-xs font-semibold bg-amber-500 text-white">Pending</span>',
        }[status] || `<span class="px-2 py-1 rounded text-xs font-semibold bg-gray-500 text-white">${status}</span>`;

        let html = `
            <div class="p-5 space-y-4">
                <div class="flex items-center justify-between">
                    <h4 class="font-semibold text-gray-900 text-lg">Run #${runId}</h4>
                    ${statusBadge}
                </div>

                <!-- Progress bar -->
                <div>
                    <div class="flex justify-between text-sm text-gray-600 mb-1">
                        <span>${completed} / ${total} nodes</span>
                        <span class="font-mono font-semibold">${pct}%</span>
                    </div>
                    <div class="w-full bg-gray-200 rounded-full h-4 overflow-hidden">
                        <div class="${barColor} h-full rounded-full transition-all duration-700 ease-out" style="width: ${pct}%"></div>
                    </div>
                </div>

                <!-- Stats grid -->
                <div class="grid grid-cols-3 gap-4 text-center">
                    <div class="p-3 bg-gray-50 rounded-lg">
                        <div class="text-2xl font-bold text-gray-900">${completed}</div>
                        <div class="text-xs text-gray-500">Completed</div>
                    </div>
                    <div class="p-3 bg-gray-50 rounded-lg">
                        <div class="text-2xl font-bold text-gray-900">${cost}</div>
                        <div class="text-xs text-gray-500">Credits</div>
                    </div>
                    <div class="p-3 bg-gray-50 rounded-lg">
                        <div class="text-2xl font-bold text-gray-900">${etaDisplay}</div>
                        <div class="text-xs text-gray-500">ETA</div>
                    </div>
                </div>

                ${currentNode !== '—' ? `
                <div class="flex items-center gap-2 text-sm text-primary bg-primary/10 p-3 rounded-lg">
                    <svg class="animate-spin h-4 w-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
                    </svg>
                    Currently running: <span class="font-semibold">${currentNode}</span>
                </div>` : ''}

                <div class="pt-2">
                    <button
                        onclick="window.logsPage.viewWorkflowRunDetails(${runId})"
                        class="text-sm text-primary hover:text-primary/80"
                    >
                        ← Back to Details
                    </button>
                </div>
            </div>
        `;

        content.innerHTML = html;
        modal.classList.remove('hidden');

    } catch (error) {
        console.error('Error loading workflow progress:', error);
        if (error.status === 404) {
            showToast('No progress data available yet', 'info', 3000);
        } else {
            showToast(`Error loading progress: ${error.message}`, 'error', 5000);
        }
    }
}

function getStatusColor(status) {
    const colors = {
        'completed': 'bg-green-600 text-white',
        'success': 'bg-green-600 text-white',
        'running': 'bg-sky-600 text-white',
        'failed': 'bg-red-600 text-white',
        'queued': 'bg-amber-500 text-white'
    };
    return colors[status] || 'bg-gray-500 text-white';
}

/**
 * View workflow tasks for a DAG run
 * View workflow tasks (backward compatibility wrapper)
 */
async function viewWorkflowTasks(nodeKind, dagRunId) {
    // Redirect to the DB-backed detail view
    await viewWorkflowDetails(nodeKind, dagRunId);
}

// Export functions for global access
window.logsPage = {
    viewWorkflowDetails,
    viewWorkflowLogs,
    viewTaskLogs,
    analyzeWithAI,
    viewWorkflowRunDetails,
    viewWorkflowRunProgress,
    viewWorkflowTasks,
    loadContainerLogs
};

// Also expose globally since onclick attributes can't access module scope
window.loadContainerLogs = loadContainerLogs;

async function viewSystemTaskLogs(taskId, nodeId) {
    try {
        const data = await apiCall(`/workers/tasks/${taskId}/container-logs`);
        const logs = (data.lines && data.lines.length > 0) ? data.lines.join('\n') : '(No logs captured)';

        // Use the task logs modal
        const logDagId = document.getElementById('log-dag-id');
        const logTaskId = document.getElementById('log-task-id');
        const logRunId = document.getElementById('log-run-id');
        const logTryNumber = document.getElementById('log-try-number');
        const logContent = document.getElementById('task-log-content');

        if (logDagId) logDagId.textContent = nodeId || 'system';
        if (logTaskId) logTaskId.textContent = taskId;
        if (logRunId) logRunId.textContent = 'system';
        if (logTryNumber) logTryNumber.textContent = '1';
        if (logContent) logContent.textContent = logs;

        // Wire the AI Assistant panel for this system task. The shared
        // task-logs modal hosts the panel, but this opener previously
        // never initialised it, so the helper sat dead on System Tasks.
        // Real container logs are already in #task-log-content (the
        // panel's default logTailGetter target), so it has genuine data.
        initLogsLLMPanel(taskId);

        document.getElementById('task-logs-modal')?.classList.remove('hidden');
    } catch (err) {
        showError('Failed to load logs: ' + err.message);
    }
}
window.viewSystemTaskLogs = viewSystemTaskLogs;


// ========================================
// Log Streaming tab (0b.2 redo R37.4 part 2)
// ========================================
// The other Logs panels are inline markup in pages/logs.html; this child
// keeps its own page file, so its markup is fetched verbatim into
// #logs-panel-log-streams (same fetch+innerHTML mechanism ui.js uses for
// standalone pages) and js/log-streams.js's own initializer then runs
// untouched against those ids. Loaded ONCE per page load — re-clicking the
// tab (or hitting the shared Refresh button) only toggles visibility;
// re-initializing would double-bind the child's own listeners.
let _logStreamsPanelLoaded = false;

async function loadLogStreamsPanel() {
    if (_logStreamsPanelLoaded) return;
    if (!(await isPaidEdition())) return; // CE: no SIEM tab (Tim B3 #9)
    _logStreamsPanelLoaded = true;
    const panel = document.getElementById('logs-panel-log-streams');
    if (!panel) return;
    fetch('/pages/log-streams.html').then((res) => res.ok ? res.text() : null)
        .then((markup) => {
            if (markup && panel) panel.innerHTML = markup;
            return initializeLogStreamsPage();
        })
        .catch(() => { /* js/log-streams.js already fails soft in-page */ });
}


// ========================
// System Tasks
// ========================

let systemTaskPage = 0;
const systemTaskPageSize = 50;

async function loadSystemTasks() {
    const tbody = document.getElementById('system-tasks-tbody');
    if (!tbody) return;

    tbody.innerHTML = '<tr><td colspan="7" class="py-8 text-center text-gray-500">Loading...</td></tr>';

    try {
        const data = await apiCall(`/workers/tasks/system-tasks?limit=${systemTaskPageSize}&offset=${systemTaskPage * systemTaskPageSize}`);
        const tasks = data.tasks || [];

        if (tasks.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="py-8 text-center text-gray-500">No system tasks found</td></tr>';
            return;
        }

        // Terminal rows render PASS or FAIL — never just "completed".
        // The ApiTaskStatus enum's serialized values are lowercase
        // (`completed`, `failed`, …); normalize to uppercase before
        // comparing so the labels actually take effect. Without this
        // every row fell through to the default else and rendered the
        // raw lowercase status string — Tim's #13 report.
        // COMPLETED + no error  → PASS
        // COMPLETED + error_message, FAILED, EXPIRED, CANCELLED → FAIL
        // PENDING, ASSIGNED, CLAIMED, RUNNING → In progress
        const TERMINAL_FAIL = new Set(['FAILED', 'EXPIRED', 'CANCELLED']);
        const IN_FLIGHT = new Set(['PENDING', 'ASSIGNED', 'CLAIMED', 'RUNNING']);

        tbody.innerHTML = tasks.map(t => {
            const status = String(t.status || '').toUpperCase();
            let statusClass, statusLabel, statusIcon;
            if (status === 'COMPLETED' && !t.error_message) {
                statusClass = 'bg-green-600 text-white';
                statusLabel = 'PASS';
                statusIcon = '&#x2705; ';
            } else if (status === 'COMPLETED' || TERMINAL_FAIL.has(status)) {
                statusClass = 'bg-red-600 text-white';
                statusLabel = 'FAIL';
                statusIcon = '&#x274C; ';
            } else if (IN_FLIGHT.has(status)) {
                statusClass = 'bg-sky-600 text-white';
                statusLabel = 'In progress';
                statusIcon = '&#x23F3; ';
            } else {
                statusClass = 'bg-gray-500 text-white';
                statusLabel = status || 'UNKNOWN';
                statusIcon = '';
            }
            const image = t.processing_image ? t.processing_image.split('/').pop() : t.node_kind || '-';
            const taskName = (t.node_id || '-').replace('system-', '').replace(/_/g, ' ');
            const time = t.completed_at || t.created_at || '-';
            const timeStr = time !== '-' ? new Date(time).toLocaleString() : '-';
            // Calculate duration
            let duration = '';
            if (t.created_at && t.completed_at) {
                const ms = new Date(t.completed_at) - new Date(t.created_at);
                duration = ms < 1000 ? `${ms}ms` : `${(ms/1000).toFixed(1)}s`;
            }
            return `<tr class="hover:bg-gray-50">
                <td class="px-4 py-3 text-sm text-gray-600">${t.id}</td>
                <td class="px-4 py-3 text-sm font-medium text-gray-900 capitalize">${taskName}</td>
                <td class="px-4 py-3 text-sm text-gray-600 font-mono text-xs">${image}</td>
                <td class="px-4 py-3">
                    <span class="px-2 py-0.5 text-xs rounded-full ${statusClass}"${statusLabel === 'FAIL' && t.error_message ? ` title="${(t.error_message || '').replace(/"/g, '&quot;')}"` : ''}>${statusIcon}${statusLabel}</span>
                    ${t.log_summary ? `<div class="text-[10px] text-gray-500 mt-1 truncate max-w-[200px]" title="${t.log_summary}">${t.log_summary}</div>` : ''}
                </td>
                <td class="px-4 py-3 text-sm text-gray-600">${t.claimed_by_worker || 'API'}</td>
                <td class="px-4 py-3 text-sm text-gray-500">${timeStr}${duration ? ` <span class="text-gray-400">(${duration})</span>` : ''}</td>
                <td class="px-4 py-3 text-right">
                    <button onclick="viewSystemTaskLogs(${t.id}, '${taskName}')" class="text-primary hover:text-primary/80 text-sm">Details</button>
                    ${t.error_message ? `<div class="text-xs text-red-500 mt-1 max-w-xs truncate" title="${t.error_message}">${t.error_message}</div>` : ''}
                </td>
            </tr>`;
        }).join('');
        // Pagination controls
        const total = data.total || tasks.length;
        const start = systemTaskPage * systemTaskPageSize + 1;
        const end = Math.min(start + tasks.length - 1, total);
        const hasPrev = systemTaskPage > 0;
        const hasNext = end < total;

        let paginationEl = document.getElementById('system-tasks-pagination');
        if (!paginationEl) {
            paginationEl = document.createElement('div');
            paginationEl.id = 'system-tasks-pagination';
            paginationEl.className = 'px-4 py-3 border-t border-gray-200 flex items-center justify-between';
            tbody.closest('table').parentElement.appendChild(paginationEl);
        }
        paginationEl.innerHTML = `
            <span class="text-sm text-gray-600">Showing ${start}-${end} of ${total}</span>
            <div class="flex gap-2">
                <button id="prev-system-tasks" class="px-3 py-1 bg-white border border-gray-300 rounded text-sm hover:bg-gray-50 ${hasPrev ? '' : 'opacity-50 cursor-not-allowed'}" ${hasPrev ? '' : 'disabled'}>Previous</button>
                <button id="next-system-tasks" class="px-3 py-1 bg-white border border-gray-300 rounded text-sm hover:bg-gray-50 ${hasNext ? '' : 'opacity-50 cursor-not-allowed'}" ${hasNext ? '' : 'disabled'}>Next</button>
            </div>
        `;
        document.getElementById('prev-system-tasks')?.addEventListener('click', () => { systemTaskPage--; loadSystemTasks(); });
        document.getElementById('next-system-tasks')?.addEventListener('click', () => { systemTaskPage++; loadSystemTasks(); });

    } catch (error) {
        console.error('Error loading system tasks:', error);
        tbody.innerHTML = `<tr><td colspan="7" class="py-8 text-center text-red-500">Error: ${escapeHtml(error.message)}</td></tr>`;
    }
}

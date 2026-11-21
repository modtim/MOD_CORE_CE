// hub-talk-to-task.js — GROUP L 20.6.s3: the "MOD Talk → Plane" review tab.
// Owner rule 10-08: hub TABS inside an existing page, never a new sidebar row —
// this tab rides the Logs page's existing tab strip (pages/logs.html #logs-tab-talk-
// task + #logs-panel-talk-to-task), the hub that already owns Workflow Runs.
// logs.js's generic `.logs-tab` click wiring switches the panel; this module only
// fills it. No module-level DOM access: everything runs inside the exported init.
//
// Read-only list + a decision, using ONLY the existing Gate-5 approval routes
// (CORE/API/routers/interventions.py, mounted at "" in main.py:1621 — the router
// carries its own /interventions prefix, so these paths carry no /v1):
//   GET  /interventions?limit=        what is parked on a human_approval node FOR
//                                     THE CALLER (id, node_run_id, workflow_run_id,
//                                     workflow_id, status 'pending_action'|'action_taken'
//                                     |'rejected'|'escalated', instructions, assigned_*)
//   POST /interventions/{id}/approve body {notes} — completes the parked NodeRun,
//                                     the DAG resumes (workflow_orchestrator)
//   POST /interventions/{id}/reject  body {notes} — fails the run
// A second decision is a 409, an approval assigned to someone else a 403; the
// server's own words are shown inline in #tt-error, never swallowed.
//
// The runs themselves come from the existing Dashboard runs endpoint GET
// /road/runs (the one js/logs.js and the Home widgets already call); the tab
// filters CLIENT-SIDE on workflow_name === 'MOD Talk → Plane' because that route
// has no name filter. include_details=true carries run.node_runs[].outputs, i.e.
// the Task merge node's proposed Plane tasks (new issue / sub-issue under X /
// comment on X).
//
// Every server string is written with textContent — innerHTML never carries data.
import { fetchApi as apiCall } from './api.js';

const WORKFLOW_NAME = 'MOD Talk → Plane';
const EMPTY_TEXT = 'No MOD Talk runs are waiting for review. The MOD Talk → Plane '
    + 'workflow is planned; once it runs, its proposed Plane tasks appear here.';
const RUN_LIMIT = 200;
const IV_LIMIT = 200;

function _errText(e) {
    if (!e) return '';
    const detail = e.detail !== undefined ? e.detail : e.message;
    if (Array.isArray(detail)) {
        return detail.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join('; ');
    }
    return typeof detail === 'string' ? detail : JSON.stringify(detail);
}

export async function initializeTalkToTaskPanel() {
    const panel = document.getElementById('logs-panel-talk-to-task');
    if (!panel) return;

    // BUG.B90 — logs.js calls this initializer on EVERY tab activation and every
    // Refresh click (not just once), so it must be idempotent: reuse the boxes
    // already in the panel instead of appending a fresh #tt-error/#tt-list pair
    // each time. Re-appending on every call stacked N copies of the empty-state
    // text (or N copies of real rows) in the panel.
    let errorBox = panel.querySelector('#tt-error');
    let listEl = panel.querySelector('#tt-list');
    if (!errorBox) {
        errorBox = document.createElement('div');
        errorBox.id = 'tt-error';
        errorBox.className = 'hidden bg-red-50 border rounded-lg p-4 mb-4 text-sm text-red-700';
        panel.appendChild(errorBox);
    }
    if (!listEl) {
        listEl = document.createElement('div');
        listEl.id = 'tt-list';
        panel.appendChild(listEl);
    }

    const runsById = new Map();

    function showError(msg) {
        if (!msg) return;
        errorBox.textContent = msg;
        errorBox.classList.remove('hidden');
    }

    async function load() {
        errorBox.textContent = '';
        errorBox.classList.add('hidden');
        listEl.innerHTML = '';

        let runs = [];
        let interventions = [];
        try {
            runs = await apiCall(`/road/runs?limit=${RUN_LIMIT}&include_details=true`);
        } catch (e) {
            showError(_errText(e));
            return;
        }
        try {
            interventions = await apiCall(`/interventions?limit=${IV_LIMIT}`);
        } catch (e) {
            showError(_errText(e));
            return;
        }

        (runs || []).forEach((r) => {
            if (r && r.workflow_name === WORKFLOW_NAME) runsById.set(r.id, r);
        });

        const rows = (interventions || []).filter(
            (iv) => iv && iv.status === 'pending_action' && runsById.has(iv.workflow_run_id));

        if (!rows.length) {
            const p = document.createElement('p');
            p.className = 'text-sm text-gray-500';
            p.textContent = EMPTY_TEXT;
            listEl.appendChild(p);
            return;
        }

        for (const iv of rows) listEl.appendChild(renderRow(iv, runsById.get(iv.workflow_run_id)));
    }

    function renderRow(iv, run) {
        const card = document.createElement('div');
        card.className = 'bg-white border rounded-lg p-4 mb-4';

        const head = document.createElement('div');
        head.className = 'flex items-center justify-between mb-2';
        const title = document.createElement('div');
        title.className = 'text-sm font-medium text-gray-700';
        title.textContent = `Run #${iv.workflow_run_id} — waiting on human approval`
            + (iv.instructions ? `: ${iv.instructions}` : '');
        const when = document.createElement('div');
        when.className = 'text-xs text-gray-500';
        when.textContent = iv.assigned_at ? `parked ${iv.assigned_at}` : '';
        head.appendChild(title);
        head.appendChild(when);
        card.appendChild(head);

        card.appendChild(proposedTasks(run));
        card.appendChild(decisionButtons(iv));
        return card;
    }

    // The Task merge node's output = the proposed Plane tasks. The run's node_runs
    // carry node_id (graph_node_id from workflow_node); the merge stage is the one
    // whose graph id mentions "merge".
    function proposedTasks(run) {
        const wrap = document.createElement('div');
        wrap.className = 'text-sm text-gray-700 mb-2';
        const nodeRuns = run?.node_runs || [];
        const merge = nodeRuns.filter((nr) => {
            const gid = (nr.node_id || nr.workflow_node?.graph_node_id || '').toLowerCase();
            return gid.includes('merge');
        });
        if (!merge.length) {
            const p = document.createElement('p');
            p.className = 'text-xs text-gray-500';
            p.textContent = 'No Task merge output on this run yet.';
            wrap.appendChild(p);
            return wrap;
        }
        for (const nr of merge) {
            for (const out of (nr.outputs || [])) {
                const line = document.createElement('div');
                line.className = 'text-xs text-gray-700';
                line.textContent = typeof out === 'string' ? out : JSON.stringify(out);
                wrap.appendChild(line);
            }
        }
        return wrap;
    }

    function decisionButtons(iv) {
        const wrap = document.createElement('div');
        wrap.className = 'flex items-center gap-2 mt-2';
        for (const verb of ['approve', 'reject']) {
            const btn = document.createElement('button');
            btn.className = 'px-3 py-1 rounded-md text-sm font-medium text-primary';
            btn.setAttribute('aria-label',
                `${verb === 'approve' ? 'Approve' : 'Reject'} run ${iv.workflow_run_id}`);
            btn.textContent = verb === 'approve' ? 'Approve' : 'Reject';
            btn.addEventListener('click', () => decide(iv.id, verb));
            wrap.appendChild(btn);
        }
        return wrap;
    }

    async function decide(interventionId, verb) {
        try {
            await apiCall(`/interventions/${encodeURIComponent(interventionId)}/${verb}`,
                { method: 'POST',
                  body: JSON.stringify({ notes: `Dashboard Talk → Plane tab: ${verb}` }) });
            await load();
        } catch (e) {
            showError(_errText(e));
        }
    }

    await load();
}

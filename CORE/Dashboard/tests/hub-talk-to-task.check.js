// hub-talk-to-task.check.js — GROUP L 20.6.s3 static check (no browser).
// Run: node CORE/Dashboard/tests/hub-talk-to-task.check.js
//
// Asserts: (1) the review tab lives in the EXISTING hub that owns workflow runs
// (pages/logs.html tab strip + panel), (2) NO new sidebar row was added (diff-free
// against pages/dashboard-shell.html), (3) every fetch path carries no '/v1',
// (4) Approve/Reject hit the REAL Gate-5 routes in CORE/API/routers/interventions.py,
// (5) the empty-state text is the planned-workflow wording, (6) the module never
// touches DOM at module level.
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const ROOT = path.join(__dirname, '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

const tabJs = read('js/hub-talk-to-task.js');
const logsHtml = read('pages/logs.html');
const logsJs = read('js/logs.js');
const shell = read('pages/dashboard-shell.html');
const interventionsPy = path.join(ROOT, '..', 'API', 'routers', 'interventions.py');
const interventionsSrc = fs.readFileSync(interventionsPy, 'utf8');

// CE-extract bail (FIX-CE-DASH-SWEEP-D): the talk-to-task / ASR seeds are denied in the
// CE cut (manifest deny block ~1334: nimbridge / planenode / taskextractnode /
// taskmergenode have no CE build, so the workflow, the stack and the tab are not seeded),
// and the scrub row at manifest 4126 drops the #logs-tab-talk-to-task button markup from
// pages/logs.html. The hub this check drives is therefore not in the shipped shape. Bail
// ONLY when the seed source is absent; on the paid tree every assert below runs unchanged.
if (!fs.existsSync(path.join(ROOT, '..', 'API', 'seed_talk_to_task_workflow.py'))) {
    console.log('SKIP hub-talk-to-task: CORE/API/seed_talk_to_task_workflow.py is not in this tree '
        + '(CE cut: the talk-to-task workflow is not seeded, the tab markup is scrubbed away)');
    process.exit(0);
}

// --- 1) the tab lives in the existing hub (Logs page owns Workflow Runs) ----
assert.ok(/id="logs-tab-workflows"/.test(logsHtml),
    'the hub must be the page that owns Workflow Runs');
assert.ok(/id="logs-tab-talk-to-task"/.test(logsHtml),
    'pages/logs.html: #logs-tab-talk-to-task button missing');
assert.ok(/id="logs-panel-talk-to-task" class="logs-panel hidden"/.test(logsHtml),
    'pages/logs.html: #logs-panel-talk-to-task panel missing (must start hidden)');
assert.ok(/id="logs-tab-workflows"|id="logs-tab-system"|id="logs-tab-log-streams"/.test(logsHtml),
    'the existing tabs must survive');

// --- 2) NO new sidebar row (diff-free against the sidebar file) -------------
assert.ok(!/id="talk-to-task-nav-link"/.test(shell),
    'dashboard-shell.html: a new sidebar row was added (owner rule: hub TABS only)');
assert.ok(!/href="#talk-to-task"/.test(shell),
    'dashboard-shell.html: no #talk-to-task nav href may exist');
assert.ok(!/talk-to-task/.test(shell),
    'dashboard-shell.html: the sidebar file must be untouched by this tab');

// --- 3) fetch paths never include /v1 --------------------------------------
const fetchPaths = tabJs.match(/apiCall\(\s*`[^`]*`/g) || [];
assert.ok(fetchPaths.length >= 3, 'hub-talk-to-task.js: expected runs + interventions + decision calls');
for (const call of fetchPaths) {
    assert.ok(!/\/v1\b/.test(call), `hub-talk-to-task.js: fetch path contains /v1 -> ${call}`);
}
assert.ok(!/window\.APP_CONFIG/.test(tabJs) || /API_URL/.test(tabJs),
    'hub-talk-to-task.js: if a base URL is spelled out it must come from window.APP_CONFIG.API_URL');
assert.ok(/from '\.\/api\.js'/.test(tabJs),
    'hub-talk-to-task.js: must go through the shared api.js client (fetchApi/apiCall)');

// --- 4) Approve/Reject call the REAL Gate-5 routes -------------------------
assert.ok(/@router\.post\("\/\{intervention_id\}\/approve"/.test(interventionsSrc)
    && /@router\.post\("\/\{intervention_id\}\/reject"/.test(interventionsSrc),
    'CORE/API/routers/interventions.py must carry the approve/reject routes (they do)');
assert.ok(/router = APIRouter\(prefix="\/interventions"/.test(interventionsSrc),
    'the router prefix is /interventions');
assert.ok(/\/interventions\/\$\{encodeURIComponent\(interventionId\)\}\/\$\{verb\}/.test(tabJs),
    'hub-talk-to-task.js: decisions must POST /interventions/{id}/approve|reject');
assert.ok(/apiCall\(`\/interventions\?limit=/.test(tabJs),
    'hub-talk-to-task.js: the waiting list must come from GET /interventions');
assert.ok(/\/road\/runs\?/.test(tabJs),
    'hub-talk-to-task.js: runs must come from the existing GET /road/runs list');
assert.ok(/workflow_name === WORKFLOW_NAME/.test(tabJs)
    && /WORKFLOW_NAME = 'MOD Talk → Plane'/.test(tabJs),
    'client-side filter on the workflow name "MOD Talk → Plane"');

// --- 5) empty-state text ---------------------------------------------------
assert.ok(/No MOD Talk runs are waiting for review\./.test(tabJs),
    'empty state must say no MOD Talk runs are waiting for review');
assert.ok(/workflow is planned; once it runs, its proposed Plane tasks appear here\./.test(tabJs),
    'empty state must name the planned MOD Talk → Plane workflow and its Plane tasks');

// --- 6) no module-level DOM access -----------------------------------------
const initLine = tabJs.split('\n').findIndex((l) => /export async function initializeTalkToTaskPanel/.test(l));
const firstDom = tabJs.split('\n').findIndex((l) => /document\./.test(l));
assert.ok(initLine >= 0 && firstDom > initLine,
    'hub-talk-to-task.js: no DOM access before the exported initializer (module level stays DOM-free)');
assert.ok(/export async function initializeTalkToTaskPanel/.test(tabJs),
    'hub-talk-to-task.js: the initializer must be exported');
assert.ok(/import \{ initializeTalkToTaskPanel \} from '\.\/hub-talk-to-task\.js'/.test(logsJs),
    'js/logs.js: must import the tab initializer');
assert.ok(/logs-panel-talk-to-task/.test(logsJs),
    'js/logs.js: must activate the tab panel');

// --- 7) tailwind rule: no bg-blue-600 / text-blue-600 ----------------------
assert.ok(!/bg-blue-600|text-blue-600/.test(tabJs + logsHtml),
    'no bg-blue-600/text-blue-600 (use text-primary / bg-primary / bg-sky-600)');

// --- 8) BUG.B90 — initializer must be idempotent ----------------------------
// logs.js calls initializeTalkToTaskPanel() on EVERY tab click and Refresh
// click (see js/logs.js), not just once. The initializer must therefore reuse
// any #tt-error/#tt-list already in the panel instead of unconditionally
// creating + appending a fresh pair each call — the old code did the latter,
// which stacked N copies of the empty-state text (or N copies of real rows)
// in #logs-panel-talk-to-task.
assert.ok(/panel\.querySelector\(\s*['"]#tt-error['"]\s*\)/.test(tabJs)
    && /panel\.querySelector\(\s*['"]#tt-list['"]\s*\)/.test(tabJs),
    'hub-talk-to-task.js: initializer must look for existing #tt-error/#tt-list ' +
    'before creating new ones (BUG.B90 — repeated calls must not stack panels)');
assert.ok(/if \(!errorBox\)/.test(tabJs) && /if \(!listEl\)/.test(tabJs),
    'hub-talk-to-task.js: must only createElement+appendChild when the box is ' +
    'missing, not on every call (BUG.B90)');

// logs.js calls the initializer more than once by design (tab click + Refresh
// click) — confirm that design is still true so this guard stays meaningful.
const ttInitCalls = (logsJs.match(/initializeTalkToTaskPanel\(\)/g) || []).length;
assert.ok(ttInitCalls >= 2,
    'js/logs.js: expected initializeTalkToTaskPanel() to be called from both ' +
    'the tab-click handler and the Refresh handler (if this drops to 1, the ' +
    'BUG.B90 idempotency guard above may no longer be exercised)');

console.log('hub-talk-to-task.check.js: OK (' + ttInitCalls + ' call sites, idempotent init confirmed)');

console.log('hub-talk-to-task: ' +
    'tab in the Logs hub, sidebar untouched, /v1-free paths, real /interventions routes, '
    + 'empty state, no module-level DOM — OK');

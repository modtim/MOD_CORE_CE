'use strict';
// FIX-E2E-DASH-WORKER-APPROVE-PENDING-ROW - the
// e2e leg dash-worker-approve.spec.ts seeds a PENDING enrolment
// (POST /workers/enrollment-tokens auto_approve:false -> POST /workers/enroll/start)
// and then polls `tr[data-worker-name="<name>"]` inside #workers-table-body.
// The cr15 room run was RED there (0 passed / 1 failed): the row never rendered.
//
// This check pins the TWO shipped halves that make that row exist, VERBATIM:
//   P1 the API appends a synthetic row for a PENDING enrolment that has no
//      WorkerLicense row (routers/worker_management.py pending_enrolments loop)
//      - static guard on the shipped router source.
//   P2 that synthetic row's shape (id NEGATIVE, hardware_specs/queues null,
//      no owner_type, enrollment_status 'pending') still renders a
//      data-worker-name row through the shipped renderWorkerRow + the shipped
//      pending branch of getActions, and that row carries the Approve button.
//   P3 the row key the spec's locator uses is the raw worker_name (quotes
//      escaped), so the locator can match.
//   M1 mutation pass: strip the pending branch from getActions IN MEMORY and
//      re-render -> the Approve button MUST vanish (the check is sensitive).
//   M2 mutation pass: drop the pending_enrolments append from the router text
//      -> P1 MUST go red.
//
//   node --test CORE/Dashboard/tests/pending-worker-row-renders.check.js
//
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const workersSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'workers.js'), 'utf8');
const routerSrc = fs.readFileSync(
    path.join(__dirname, '..', '..', 'API', 'routers', 'worker_management.py'), 'utf8');

// --- P1: the shipped router lists PENDING enrolments as rows --------------
const appendMarker = 'for enrollment in pending_enrolments:';
assert.ok(routerSrc.includes(appendMarker),
    'P1 router does not append pending_enrolments rows to GET /workers');
assert.ok(routerSrc.includes('enrollment_status=enrollment.status.value'),
    'P1 the appended row does not carry enrollment_status');
const crudSrc = fs.readFileSync(
    path.join(__dirname, '..', '..', 'API', 'crud', 'worker_management.py'), 'utf8');
assert.ok(crudSrc.includes('def list_pending_enrollments_without_license'),
    'P1 the pending-enrolment query is missing');

// --- extract the shipped row builder + the shipped pending action branch ---
const rStart = workersSrc.indexOf('function renderWorkerRow(worker) {');
assert.ok(rStart >= 0, 'renderWorkerRow not found');
const rEnd = workersSrc.indexOf(
    '// Compact inline version display so the operator sees the agent version', rStart);
assert.ok(rEnd > rStart, 'renderWorkerRow end marker not found');
const rowBlock = workersSrc.slice(rStart, rEnd);

const bStart = workersSrc.indexOf("if (worker.enrollment_status === 'pending') {");
assert.ok(bStart >= 0, 'pending action branch not found in workers.js');
const bEnd = workersSrc.indexOf('// FIX-CE-WORKER-APPROVE-BUTTON END', bStart);
assert.ok(bEnd > bStart, 'pending action end marker not found');
const pendingBlock = workersSrc.slice(bStart, bEnd);

const hStart = workersSrc.indexOf('function escapeHtml(text) {');
assert.ok(hStart >= 0, 'escapeHtml not found');
const hEnd = workersSrc.indexOf('// jsNumStr', hStart);
const helperBlock = workersSrc.slice(hStart, hEnd > hStart ? hEnd : hStart + 4000);

function renderRow(source, worker) {
    const fn = new Function('worker', `
        const document = { createElement: (t) => ({ classList: { add: () => {}, remove: () => {} }, setAttribute: () => {}, appendChild: () => {}, innerHTML: '', textContent: '' }) };
        ${helperBlock}
        function getActions(w) {
            ${pendingBlock}
            return '';
        }
        function getStatusBadge() { return ''; }
        function getHeartbeatWarningBadges() { return ''; }
        function getProfilingBadge() { return ''; }
        function getVersionBadge() { return ''; }
        function getHostExecBadge() { return ''; }
        function getHardwareInfo() { return ''; }
        function getArchInfo() { return ''; }
        function getQueuesInfo() { return ''; }
        function getActivityInfo() { return ''; }
        function getOwnerTypeInfo() { return ''; }
        function getLicenseInfo() { return ''; }
        ${source}
        return renderWorkerRow(worker);
    `);
    return fn(worker);
}

// The payload the shipped router actually sends for a manual enrolment.
const NAME = 'e2e-approve-1700000000-ab12cd';
const pendingPayload = {
    id: -4242,
    worker_name: NAME,
    hostname: NAME,
    hardware_specs: null,
    queues: null,
    status: 'unlicensed',
    licensed_at: null,
    licensed_by_username: null,
    first_seen_at: '2026-11-15T00:00:00Z',
    last_heartbeat: null,
    is_online: false,
    notes: null,
    enrollment_status: 'pending',
};

const row = renderRow(rowBlock, pendingPayload);

// --- P2/P3: the row renders, is keyed by the name, and carries Approve ----
assert.ok(row.includes(`data-worker-name="${NAME}"`),
    'P3 the rendered row is not keyed by the raw worker_name');
assert.ok(row.includes('data-worker-id="-4242"'),
    'P2 the negative enrolment id is not rendered');
assert.ok(row.includes('approveWorker(') && /Approve</.test(row),
    'P2 the pending row carries no Approve button');
assert.ok(!row.includes('undefined'),
    'P2 a null field leaked "undefined" into the row');

// --- M1: mutation - pending branch removed -> no Approve button -----------
const mutated = rowBlock.replace(/const actions = getActions\(worker\);/,
    'const actions = "";');
assert.notStrictEqual(mutated, rowBlock, 'M1 mutation anchor missing (check is vacuous)');
const mutatedRow = renderRow(mutated, pendingPayload);
assert.ok(!mutatedRow.includes('approveWorker('),
    'M1 mutation did not turn the Approve button off (check insensitive)');
assert.ok(mutatedRow.includes(`data-worker-name="${NAME}"`),
    'M1 the row itself must still render (only the button is removed)');

// --- M2: mutation - router append dropped -> P1 red -----------------------
const mutatedRouter = routerSrc.replace(appendMarker, 'for enrollment in []:');
assert.notStrictEqual(mutatedRouter, routerSrc, 'M2 mutation anchor missing');
assert.ok(!mutatedRouter.includes(appendMarker), 'M2 mutation not applied');

console.log('pending-worker-row-renders: P1/P2/P3 + M1/M2 all pass');

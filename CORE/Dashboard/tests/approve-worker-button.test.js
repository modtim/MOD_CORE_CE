'use strict';
// FIX-CE-WORKER-APPROVE-BUTTON (lane gpu1-2, 11-11) - the Dashboard must offer
// an Approve button on PENDING worker enrolment rows (CE enrolment defaults to
// PENDING: CORE/API/services/worker_management_service.py only enrols APPROVED
// when the token had auto_approve=true, and the Add Worker checkbox is
// unchecked by default). Modelled on reinstateWorker; NO edition gate (the
// approve route is CE-allowed and identical on paid).
//
// Technique (as workers-unlimited-display.check.js): the shipped helpers, the
// pending-row button snippet and approveWorker are extracted VERBATIM from
// js/workers.js and run in a Function sandbox with a stub fetch/document.
//
//   node --test CORE/Dashboard/tests/approve-worker-button.test.js
//
// Asserts:
//   A1 pending fixture  -> row contains approveWorker( and label Approve
//   A2 approved/revoked/rejected fixtures -> no approveWorker( in the row
//   A3 name with a quote + HTML char -> escaped through jsAttrStr, no raw
//      injection reaches the onclick attribute
//   A4 approveWorker issues POST /workers/enroll/approve/<encoded name> with
//      the Bearer header, calls loadWorkers() on success
//   A5 non-ok response -> showError called with the detail, loadWorkers NOT
//   A6 approveWorker source carries no isPaidEdition / is_ce gate string;
//      window.approveWorker exposure line present (inline onclick)
//   A7 in-flight guard: rapid double-click -> ONE fetch; retry allowed after
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(
    path.join(__dirname, '..', 'js', 'workers.js'), 'utf8');

// --- extract the shipped escape helpers (verbatim) -------------------------
const hStart = src.indexOf('function escapeHtml(text) {');
assert.ok(hStart >= 0, 'escapeHtml not found in workers.js');
const hEnd = src.indexOf('// jsNumStr', hStart);
assert.ok(hEnd > hStart, 'end marker for helper block not found');
const helperBlock = src.slice(hStart, hEnd);

// --- extract the pending-row action block (verbatim) -----------------------
const bStart = src.indexOf("if (worker.enrollment_status === 'pending') {");
assert.ok(bStart >= 0, 'pending action block not found in workers.js');
const bEnd = src.indexOf('// FIX-CE-WORKER-APPROVE-BUTTON END', bStart);
assert.ok(bEnd > bStart, 'end marker for pending action block not found');
const buttonBlock = src.slice(bStart, bEnd);

// --- extract approveWorker (verbatim) --------------------------------------
const fStart = src.indexOf('async function approveWorker(workerName) {');
assert.ok(fStart >= 0, 'approveWorker not found in workers.js');
const fEnd = src.indexOf('// FIX-CE-WORKER-APPROVE-BUTTON END', fStart);
assert.ok(fEnd > fStart, 'end marker for approveWorker not found');
const fnBlock = src.slice(fStart, fEnd);

const CASES = [];
function record(name, fn) { CASES.push({ name, fn }); }

// Tiny DOM: createElement -> an object whose innerHTML getter HTML-escapes
// whatever was assigned to textContent (what a browser does on the
// textContent -> innerHTML round-trip escapeHtml relies on).
function makeDom() {
    return {
        createElement: () => {
            const el = { _t: '' };
            Object.defineProperty(el, 'textContent', {
                get: () => el._t,
                set: (v) => { el._t = v; },
            });
            Object.defineProperty(el, 'innerHTML', {
                get: () => String(el._t)
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;'),
            });
            return el;
        },
    };
}

function renderRow(worker) {
    const dom = makeDom();
    const jsAttrStr = new Function(
        'document', helperBlock + '\nreturn jsAttrStr;'
    )(dom);
    // The shipped snippet is an early-return branch of getActions(); running
    // it standalone returns the row's action HTML for pending rows and falls
    // through to '' for every other status.
    return new Function(
        'worker', 'jsAttrStr', buttonBlock + '\nreturn "";'
    )(worker, jsAttrStr);
}

record('A1 pending row carries the Approve button', () => {
    const row = renderRow({ worker_name: 'mod-worker-embedded', enrollment_status: 'pending' });
    assert.ok(row.includes('approveWorker('), 'A1 approveWorker( missing');
    assert.ok(row.includes('>Approve</button>'), 'A1 label Approve missing');
    assert.ok(row.includes('bg-emerald-600'), 'A1 emerald class from neighbouring buttons');
});

record('A2 non-pending rows carry no Approve button', () => {
    for (const st of ['approved', 'revoked', 'rejected', 'PENDING', '']) {
        const row = renderRow({ worker_name: 'w1', enrollment_status: st });
        assert.strictEqual(row.includes('approveWorker('), false,
            `A2 ${st || '(empty)'} must not render approveWorker(`);
    }
});

record('A3 hostile name escaped through jsAttrStr', () => {
    const evil = 'x" onclick=\'alert(1)\' x="<b>';
    const row = renderRow({ worker_name: evil, enrollment_status: 'pending' });
    assert.ok(row.includes('approveWorker('), 'A3 button present');
    // The attribute value must carry NO raw quote and no raw angle bracket:
    // everything hostile is HTML-escaped by jsAttrStr, so the handler cannot
    // be closed early and no markup can be injected.
    const attr = row.slice(row.indexOf('onclick="') + 'onclick="'.length, row.indexOf('" class=')).trim();
    assert.ok(attr.startsWith('approveWorker(') && attr.endsWith(')') && attr.includes('&quot;') && attr.includes('&#39;') && attr.includes('&lt;b&gt;'), 'A3 escaping through jsAttrStr');
    assert.strictEqual(attr.includes('<'), false, 'A3 raw < in the onclick attribute');
    assert.strictEqual(attr.includes('"'), false, 'A3 raw " in the onclick attribute');
});

async function runApprove(name, ok, detail) {
    const dom = makeDom();
    const calls = [];
    let loadWorkersCalled = 0;
    let shown = [];
    const jsAttrStr = new Function('document', helperBlock + '\nreturn jsAttrStr;')(dom);
    const approveWorker = new Function(
        'workerName', 'fetch', 'showConfirm', 'showSuccess', 'showError', 'loadWorkers', 'console', 'window', 'sessionStorage', 'jsAttrStr',
        fnBlock + '\nreturn approveWorker;'
    )(
        name,
        async (url, opts) => {
            calls.push({ url, opts });
            return {
                ok,
                status: ok ? 200 : 400,
                json: async () => ok
                    ? { status: 'approved', worker_name: name }
                    : { detail },
            };
        },
        async () => true,                                    // showConfirm
        (m) => { shown.push(m); },                            // showSuccess
        (m) => { shown.push('ERR:' + m); },                   // showError
        async () => { loadWorkersCalled++; },                 // loadWorkers
        { log() {}, warn() {}, error() {} },                  // console
        { APP_CONFIG: { API_URL: 'http://api.test' } },       // window
        { getItem: () => 'tok-1' },                           // sessionStorage
        jsAttrStr
    );
    await approveWorker(name);

    return { calls, loadWorkersCalled, shown };
}

record('A4 approve POSTs the enroll/approve path with Bearer and refreshes', async () => {
    const r = await runApprove('box a"b', true, null);
    assert.strictEqual(r.calls.length, 1, 'A4 one fetch');
    const { url, opts } = r.calls[0];
    assert.strictEqual(url, 'http://api.test/workers/enroll/approve/box%20a%22b', 'A4 path + encodeURIComponent');
    assert.strictEqual(opts.method, 'POST', 'A4 method POST');
    assert.strictEqual(opts.headers.Authorization, 'Bearer tok-1', 'A4 Bearer header');
    assert.strictEqual(r.loadWorkersCalled, 1, 'A4 loadWorkers() called on success');
    assert.ok(r.shown.some((m) => m.startsWith('Worker "')), 'A4 showSuccess fired');
});

record('A5 non-ok response -> showError, no refresh', async () => {
    const r = await runApprove('w1', false, 'enrolment not pending');
    assert.strictEqual(r.loadWorkersCalled, 0, 'A5 loadWorkers NOT called on failure');
    assert.ok(r.shown.some((m) => m === 'ERR:enrolment not pending'), 'A5 showError got the detail');
});

record('A6 approveWorker source has no edition gate', async () => {
    assert.strictEqual(fnBlock.includes('isPaidEdition'), false, 'A6 isPaidEdition present');
    assert.strictEqual(fnBlock.includes('is_ce'), false, 'A6 is_ce present');
    assert.ok(buttonBlock.includes("worker.enrollment_status === 'pending'"), 'A6 button is pending-only');
    assert.ok(src.includes('window.approveWorker = approveWorker;'),
        'A6 approveWorker exposed on window for inline onclick');
});

// FIX-NIT-APPROVE-BUTTON-3 NIT 1: the in-flight guard. Two rapid calls on the
// same worker issue ONE fetch; the guard is cleared in finally so a retry
// after a failure (non-ok) is allowed.
record('A7 in-flight guard: rapid double-click issues ONE POST; retry after failure allowed', async () => {
    const calls = [];
    const win = { APP_CONFIG: { API_URL: 'http://api.test' } };
    const approveWorker = new Function(
        'workerName', 'fetch', 'showConfirm', 'showSuccess', 'showError', 'loadWorkers', 'console', 'window', 'sessionStorage', 'jsAttrStr',
        fnBlock + '\nreturn approveWorker;'
    )(
        'w1',
        async (url, opts) => {
            calls.push({ url, opts });
            const ok = false;                                    // stub answers a failure
            await new Promise((r) => setTimeout(r, 5));          // request in flight
            return {
                ok,
                status: ok ? 200 : 400,
                json: async () => ok
                    ? { status: 'approved', worker_name: 'w1' }
                    : { detail: 'enrolment not pending' },
            };
        },
        async () => true,                                        // showConfirm
        () => {},                                                // showSuccess
        () => {},                                                // showError
        async () => {},                                          // loadWorkers
        { log() {}, warn() {}, error() {} },                     // console
        win,                                                     // window (shared)
        { getItem: () => 'tok-1' },                              // sessionStorage
        null
    );
    const p1 = approveWorker('w1');
    const p2 = approveWorker('w1');                              // rapid second click
    await Promise.all([p1, p2]);
    assert.strictEqual(calls.length, 1, 'A7 double-click must issue ONE fetch');
    assert.strictEqual(win.__approveInFlight.size, 0, 'A7 guard cleared once the in-flight call settled');
    // The stub answered ok=false (nextOk stays false) -> the guard was cleared
    // in finally, so the retry is NOT blocked and issues a second fetch.
    await approveWorker('w1');
    assert.strictEqual(calls.length, 2, 'A7 retry after the failed call issues a second fetch');
    assert.strictEqual(win.__approveInFlight.size, 0, 'A7 guard cleared after the retry');
});

(async () => {
    let passed = 0;
    let failed = 0;
    for (const c of CASES) {
        try {
            await c.fn();
            passed++;
            console.log(`ok   ${c.name}`);
        } catch (e) {
            failed++;
            console.log(`FAIL ${c.name}: ${e.message}`);
        }
    }
    console.log(`${passed} passed, ${failed} failed`);
    process.exit(failed ? 1 : 0);
})();

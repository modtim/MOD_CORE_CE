'use strict';
// TEST-HARDEN-APPROVE-BUTTON (lane gpu2-1, 11-11) - hardening for the CE
// worker Approve button: CORE/Dashboard/js/workers.js approveWorker + the
// pending-row branch of getActions (the row builder). Companion to
// CORE/Dashboard/tests/approve-worker-button.test.js (cases A1..A7); these
// cases (H1..H11) cover the branches that file leaves untested.
//
// Technique (as approve-worker-button.test.js / workers-unlimited-display.check.js):
// the shipped helpers, the pending-row snippet and approveWorker are extracted
// VERBATIM from js/workers.js and run in a Function sandbox with a stub
// fetch/document, so every case exercises the SHIPPED code, not a copy.
//
//   node --test CORE/Dashboard/tests/approve-worker-hardening.test.js
//
// Cases:
//   H1  status casing: ONLY the exact lowercase 'pending' renders the button
//       ('PENDING'/'Pending'/'pending '/'approved'/'revoked'/'rejected'/''/null
//        render nothing) - the API sends the lowercase enum
//   H2  hostile name (quote/backslash/angle/ampersand/newline/unicode) stays
//       inert in the generated onclick: jsAttrStr escapes, no raw quote/angle/
//       ampersand/newline reaches the attribute, and the unescaped handler still
//       compiles as exactly ONE call carrying the original string
//   H3  confirm DECLINED -> no fetch at all, guard cleared, retry still allowed
//   H4  401/403/404/409/500 -> showError(detail), no refresh, guard cleared,
//       retry allowed (button stays usable)
//   H5  success -> loadWorkers() called exactly ONCE (list refreshed once)
//   H6  two DIFFERENT workers approved concurrently -> two fetches (the guard
//       is per-name, not global)
//   H7  fetch throws (network error) -> showError, guard cleared, retry allowed
//   H8  the name is percent-encoded in the path (space/quote/slash/ampersand/
//       unicode) - no raw separator can forge a path segment
//   H9  Authorization header built from the SAME source as reinstateWorker
//       (sessionStorage accessToken + `Bearer ${token}`)
//   H10 approved/revoked/rejected rows carry no Approve button
//   H11 window.approveWorker exposure line present (inline onclick needs it)
//
// Mutations (proven separately via MCW_OVERRIDE on the RUN tree only, never the
// repo tree; each MUST fail at least one case):
//   M1 drop the guard clear in finally   -> H3/H4/H7 fail
//   M2 drop encodeURIComponent           -> H8 fails
//   M3 render the button for every status-> H1/H10 fail
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(
    path.join(__dirname, '..', 'js', 'workers.js'), 'utf8');

const hStart = src.indexOf('function escapeHtml(text) {');
assert.ok(hStart >= 0, 'escapeHtml not found in workers.js');
const hEnd = src.indexOf('// jsNumStr', hStart);
assert.ok(hEnd > hStart, 'end marker for helper block not found');
const helperBlock = src.slice(hStart, hEnd);

const bStart = src.indexOf("if (worker.enrollment_status === 'pending') {");
assert.ok(bStart >= 0, 'pending action block not found in workers.js');
const bEnd = src.indexOf('// FIX-CE-WORKER-APPROVE-BUTTON END', bStart);
assert.ok(bEnd > bStart, 'end marker for pending action block not found');
const buttonBlock = src.slice(bStart, bEnd);

const fStart = src.indexOf('async function approveWorker(workerName) {');
assert.ok(fStart >= 0, 'approveWorker not found in workers.js');
const fEnd = src.indexOf('// FIX-CE-WORKER-APPROVE-BUTTON END', fStart);
assert.ok(fEnd > fStart, 'end marker for approveWorker not found');
const fnBlock = src.slice(fStart, fEnd);

const rStart = src.indexOf('async function reinstateWorker(workerName) {');
assert.ok(rStart >= 0 && rStart < fStart, 'reinstateWorker not found before approveWorker');
const reinstateBlock = src.slice(rStart, fStart);

const CASES = [];
function record(name, fn) { CASES.push({ name, fn }); }

// Tiny DOM: the textContent -> innerHTML round-trip escapeHtml relies on.
function makeDom() {
    return {
        createElement: () => {
            const el = { _t: '' };
            Object.defineProperty(el, 'textContent', { get: () => el._t, set: (v) => { el._t = v; } });
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

function jsAttrStrOf() {
    return new Function('document', helperBlock + '\nreturn jsAttrStr;')(makeDom());
}

function renderRow(worker) {
    const jsAttrStr = jsAttrStrOf();
    // The shipped snippet is an early-return branch of getActions(): run
    // standalone it returns the row's action HTML for pending rows and falls
    // through to '' for every other status.
    return new Function('worker', 'jsAttrStr', buttonBlock + '\nreturn "";')(worker, jsAttrStr);
}

function attrOf(row) {
    const open = row.indexOf('onclick="');
    assert.ok(open >= 0, 'no onclick attribute in the rendered row');
    const close = row.indexOf('" class=', open);
    assert.ok(close > open, 'onclick attribute is not terminated before class= (raw quote leaked)');
    return row.slice(open + 'onclick="'.length, close).trim();
}

// Sandbox around the shipped approveWorker. spec:
//   ok, status, detail, body, throws, delayMs, confirm (array of bools), token
function sandbox(spec) {
    const trace = { calls: [], shown: [], refresh: 0 };
    const win = { APP_CONFIG: { API_URL: 'http://api.test' } };
    const confirms = spec.confirm || [true];
    let n = 0;
    const fetchImpl = async (url, opts) => {
        trace.calls.push({ url, opts });
        if (spec.delayMs) await new Promise((r) => setTimeout(r, spec.delayMs));
        if (spec.throws) throw new TypeError('Failed to fetch');
        const ok = spec.ok !== false;
        return {
            ok,
            status: spec.status ?? (ok ? 200 : 400),
            json: async () => ok
                ? (spec.body ?? { status: 'approved', worker_name: 'w1' })
                : { detail: spec.detail },
        };
    };
    const confirmImpl = async () => confirms[Math.min(n++, confirms.length - 1)];
    const jsAttrStr = jsAttrStrOf();
    const escapeHtml = new Function('document', helperBlock + '\nreturn escapeHtml;')(makeDom());
    const approve = new Function(
        'workerName', 'fetch', 'showConfirm', 'showSuccess', 'showError', 'loadWorkers', 'console', 'window', 'sessionStorage', 'jsAttrStr', 'escapeHtml',
        fnBlock + '\nreturn approveWorker;'
    )(
        null, fetchImpl, confirmImpl,
        (m) => { trace.shown.push('OK:' + m); },
        (m) => { trace.shown.push('ERR:' + m); },
        async () => { trace.refresh++; },
        { log() {}, warn() {}, error() {} },
        win,
        { getItem: () => spec.token === undefined ? 'tok-1' : spec.token },
        jsAttrStr
    );
    return { approve, trace, win };
}

// --- H1: status casing / unknown statuses ---------------------------------
record('H1 only the exact lowercase pending status renders the button', () => {
    const hit = renderRow({ worker_name: 'w1', enrollment_status: 'pending' });
    assert.ok(hit.includes('approveWorker('), 'H1 pending renders the button');
    for (const st of ['PENDING', 'Pending', 'pEndInG', ' pending', 'pending ',
                      'approved', 'revoked', 'rejected', 'unknown', '', null, undefined]) {
        const row = renderRow({ worker_name: 'w1', enrollment_status: st });
        assert.strictEqual(row.includes('approveWorker('), false,
            `H1 ${st === null ? 'null' : st === undefined ? 'undefined' : JSON.stringify(st)} must not render the button`);
        assert.strictEqual(row.includes('>Approve<'), false, 'H1 no Approve label for non-pending');
    }
    // The API sends the lowercase enum value (CORE/API/models.py WorkerEnrollmentStatus
    // PENDING = "pending"), so the strict compare is correct, not a bug.
    assert.ok(src.includes("worker.enrollment_status === 'pending'"), 'H1 shipped compare is the lowercase enum');
});

// --- H2: hostile names stay inert in the generated onclick ----------------
record('H2 hostile name (quote/backslash/angle/amp/newline/unicode) stays inert in onclick', () => {
    const jsAttrStr = jsAttrStrOf();
    const names = [
        'x" onclick=\'alert(1)\' x="<b>',
        'a\\b',                       // backslash
        'a<b>c',
        'a&b',
        'a\nb',                      // newline: JSON.stringify emits \\n, not a raw break
        'a\r\nb',
        'привет-ünït',               // unicode passes through unchanged
        'a</button><button onclick="evil()>',
    ];
    for (const name of names) {
        const row = renderRow({ worker_name: name, enrollment_status: 'pending' });
        assert.ok(row.includes('approveWorker('), 'H2 button present for hostile name');
        const attr = attrOf(row);
        assert.ok(attr.startsWith('approveWorker(') && attr.endsWith(')'), 'H2 handler shape');
        assert.strictEqual(attr.includes('<'), false, 'H2 raw < in the onclick attribute');
        assert.strictEqual(attr.includes('>'), false, 'H2 raw > in the onclick attribute');
        assert.strictEqual(attr.includes('"'), false, 'H2 raw " in the onclick attribute');
        // No raw & either: every & in the attribute starts a known entity.
        assert.strictEqual(attr.replace(/&(quot|#39|lt|gt|amp);/g, '').includes('&'), false,
            'H2 raw & in the onclick attribute');
        assert.strictEqual(attr.includes('\n') || attr.includes('\r'), false, 'H2 raw newline in the attribute');
        // The escaped handler must still compile, and compile as EXACTLY ONE
        // call whose argument round-trips to the original string.
        const handler = attr.replace(/&quot;/g, '"').replace(/&#39;/g, "'")
            .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
        const seen = [];
        const stub = (a) => { seen.push(a); };
        const decoded = new Function('approveWorker', handler + ';');  // throws if the attribute broke
        decoded(stub);
        assert.strictEqual(seen.length, 1, 'H2 the handler is exactly one call');
        assert.strictEqual(seen[0], name, `H2 argument round-trips to ${JSON.stringify(name)}`);
        assert.strictEqual('approveWorker(' + jsAttrStr(name) + ')', attr,
            'H2 attribute is exactly the shipped jsAttrStr(name) interpolation');
    }
});

// --- H3: confirm declined -> no fetch at all, guard cleared, retry allowed -
record('H3 declined confirm issues ZERO fetches and leaves the guard clear', async () => {
    const s = sandbox({ confirm: [false, true] });   // 1st click declines, retry accepts
    await s.approve('w1');
    assert.strictEqual(s.trace.calls.length, 0, 'H3 no fetch after a declined confirm');
    assert.strictEqual(s.trace.refresh, 0, 'H3 no refresh after a declined confirm');
    assert.strictEqual(s.trace.shown.length, 0, 'H3 no toast after a declined confirm');
    assert.strictEqual(s.win.__approveInFlight.size, 0, 'H3 guard cleared after the decline');
    await s.approve('w1');                          // retry is NOT blocked
    assert.strictEqual(s.trace.calls.length, 1, 'H3 retry after decline issues a fetch');
});

// --- H4: every failure status -> showError, guard cleared, button usable ---
record('H4 401/403/404/409/500 -> showError(detail), no refresh, guard cleared, retry allowed', async () => {
    for (const [status, detail] of [
        [401, 'Invalid API key'],
        [403, 'Platform admin only'],
        [404, 'Worker not found'],
        [409, 'enrolment not pending'],
        [500, 'Internal Server Error'],
    ]) {
        const s = sandbox({ ok: false, status, detail });
        await s.approve('w1');
        assert.strictEqual(s.trace.calls.length, 1, `H4 ${status} one fetch`);
        assert.strictEqual(s.trace.refresh, 0, `H4 ${status} list NOT refreshed`);
        assert.ok(s.trace.shown.some((m) => m === 'ERR:' + detail), `H4 ${status} showError got the detail`);
        assert.strictEqual(s.win.__approveInFlight.size, 0, `H4 ${status} guard cleared`);
        await s.approve('w1');
        assert.strictEqual(s.trace.calls.length, 2, `H4 ${status} retry allowed (button usable)`);
    }
});

// --- H5: success refreshes the list exactly once --------------------------
record('H5 success refreshes the worker list exactly once', async () => {
    const s = sandbox({ ok: true, body: { status: 'approved', worker_name: 'w1' } });
    await s.approve('w1');
    assert.strictEqual(s.trace.calls.length, 1, 'H5 one fetch');
    assert.strictEqual(s.trace.refresh, 1, 'H5 loadWorkers() called exactly once');
    assert.ok(s.trace.shown.some((m) => m.startsWith('OK:Worker "w1" approved.')), 'H5 success toast');
});

// --- H6: two DIFFERENT workers approved concurrently ----------------------
record('H6 two different workers approved concurrently both issue their POST', async () => {
    const s = sandbox({ ok: true, delayMs: 10 });
    const p1 = s.approve('mod-worker-embedded');
    const p2 = s.approve('mod-worker-embedded');   // same name: guard blocks the 2nd
    const p3 = s.approve('worker-two');            // different name: NOT blocked
    await Promise.all([p1, p2, p3]);
    assert.strictEqual(s.trace.calls.length, 2, 'H6 one fetch per distinct worker, guard is per-name');
    const urls = s.trace.calls.map((c) => c.url);
    assert.ok(urls.some((u) => u.endsWith('/mod-worker-embedded')) && urls.some((u) => u.endsWith('/worker-two')),
        'H6 both workers got their own request');
    assert.strictEqual(s.trace.refresh, 2, 'H6 each success refreshed the list');
    assert.strictEqual(s.win.__approveInFlight.size, 0, 'H6 both guards cleared');
});

// --- H7: fetch throws (network error) -> showError, guard cleared ---------
record('H7 network error (fetch throws) -> showError, no refresh, guard cleared, retry allowed', async () => {
    const s = sandbox({ throws: true });
    await s.approve('w1');
    assert.strictEqual(s.trace.calls.length, 1, 'H7 the attempt was made');
    assert.strictEqual(s.trace.refresh, 0, 'H7 no refresh on a network error');
    assert.ok(s.trace.shown.some((m) => m === 'ERR:Failed to fetch'), 'H7 showError got the error message');
    assert.strictEqual(s.win.__approveInFlight.size, 0, 'H7 guard cleared after the throw (finally)');
    await s.approve('w1');
    assert.strictEqual(s.trace.calls.length, 2, 'H7 retry after the throw is allowed');
});

// --- H8: encodeURIComponent of the name in the path -----------------------
record('H8 the name is percent-encoded in the URL path', async () => {
    const s = sandbox({ ok: true });
    for (const name of ['box a', 'a"b', 'a/b', 'a&b', 'a b/c', 'привет', 'a%b', "a'b"]) {
        await s.approve(name);
        const url = s.trace.calls[s.trace.calls.length - 1].url;
        assert.ok(url.startsWith('http://api.test/workers/enroll/approve/'), 'H8 route prefix');
        const seg = url.slice('http://api.test/workers/enroll/approve/'.length);
        assert.strictEqual(seg, encodeURIComponent(name), 'H8 path segment is encodeURIComponent(name)');
        assert.strictEqual(seg.includes('/') && name !== 'a/b', false, 'H8 no raw slash from a name that has none');
    }
});

// --- H9: Authorization header from the same source as reinstateWorker -----
record('H9 auth header uses the same source as reinstateWorker (sessionStorage accessToken)', () => {
    const approveAuth = fnBlock.match(/headers:\s*\{\s*'Authorization':\s*`Bearer \$\{token\}`/);
    assert.ok(approveAuth, 'H9 approveWorker builds `Bearer ${token}`');
    const reinstateAuth = reinstateBlock.match(/headers:\s*\{\s*'Authorization':\s*`Bearer \$\{token\}`/);
    assert.ok(reinstateAuth, 'H9 reinstateWorker builds the same header');
    assert.ok(fnBlock.includes('sessionStorage.getItem("accessToken")'), 'H9 same token source as reinstateWorker');
    assert.ok(reinstateBlock.includes('sessionStorage.getItem("accessToken")'), 'H9 reinstateWorker token source');
    assert.ok(fnBlock.includes("'Content-Type': 'application/json'"), 'H9 same content-type as reinstateWorker');
    const s = sandbox({ ok: true, token: 'tok-xyz' });
    const r = require('node:assert'); void r;
    return (async () => {
        await s.approve('w1');
        const opts = s.trace.calls[0].opts;
        assert.strictEqual(opts.headers.Authorization, 'Bearer tok-xyz', 'H9 header value at runtime');
        assert.strictEqual(opts.method, 'POST', 'H9 method POST');
    })();
});

// --- H10: approved/revoked/rejected rows carry no Approve button ----------
record('H10 approved/revoked/rejected rows carry no Approve button', () => {
    for (const st of ['approved', 'revoked', 'rejected']) {
        const row = renderRow({ worker_name: 'w1', enrollment_status: st });
        assert.strictEqual(row.includes('approveWorker('), false, `H10 ${st}: no approve handler`);
        assert.strictEqual(row.includes('Approve'), false, `H10 ${st}: no Approve label`);
    }
});

// --- H11: window.approveWorker exposure line (inline onclick needs it) ----
record('H11 approveWorker is exposed on window for the inline onclick', () => {
    assert.ok(src.includes('window.approveWorker = approveWorker;'), 'H11 exposure line present');
    assert.ok(buttonBlock.includes('onclick="approveWorker('), 'H11 the button calls the global handler');
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
            console.log(`FAIL ${c.name}: ${String(e.message).slice(0, 200)}`);
        }
    }
    console.log(`${passed} passed, ${failed} failed`);
    process.exit(failed ? 1 : 0);
})();

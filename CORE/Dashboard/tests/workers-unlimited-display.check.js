'use strict';
// FIX-WORKERS-UNLIMITED-DISPLAY - the Community Edition
// Workers capacity card must show "Unlimited", never the raw UNLIMITED
// sentinel (1e15, CORE/API/utils/entitlements.py). Owner rule: CE has no
// quantity caps and must not show anything that looks like a cap.
//
// Technique (as ce-header-credit-pill.check.js): the shipped helpers and the
// license-summary loader are EXTRACTED VERBATIM from js/workers.js and run in
// a Function sandbox with a stub fetch and a tiny DOM.
//
//   node CORE/Dashboard/tests/workers-unlimited-display.check.js
//
// Asserts:
//   U1 limit 1e15 (sentinel)       -> available "Unlimited", limit hidden
//   U2 limit 8 (finite, paid)      -> "3 / 8" unchanged, separator visible
//   U3 limit null                  -> treated as Unlimited
//   U4 limit 1e12 threshold edge   -> Unlimited; 1e12 - 1 stays finite
//   U5 loader on CE sentinel       -> DOM shows "3 workers", "Unlimited"
//   U6 loader on finite limit 8    -> DOM shows "3" / "8", available 5
//   U7 the shipped source defines the threshold constant and marker block
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(
    path.join(__dirname, '..', 'js', 'workers.js'), 'utf8');

// --- extract the shipped helper block --------------------------------------
const hStart = src.indexOf('// FIX-WORKERS-UNLIMITED-DISPLAY (cloud-h5, 11-10) BEGIN');
assert.ok(hStart >= 0, 'helper block marker not found in workers.js');
const hEnd = src.indexOf('// FIX-WORKERS-UNLIMITED-DISPLAY END', hStart);
assert.ok(hEnd > hStart, 'helper block end marker not found');
const helperBlock = src.slice(hStart, hEnd);

// --- extract the loader (verbatim) -----------------------------------------
const lStart = src.indexOf('async function loadLicenseSummary() {');
assert.ok(lStart >= 0, 'loadLicenseSummary not found in workers.js');
const lEnd = src.indexOf('\nasync function ', lStart + 10);
assert.ok(lEnd > lStart, 'end marker for loadLicenseSummary not found');
const loaderBlock = src.slice(lStart, lEnd);

const helpers = new Function(
    helperBlock + '\nreturn { isUnlimitedWorkerLimit, formatWorkerCapacity, WORKER_LIMIT_UNLIMITED_THRESHOLD };'
)();
const { isUnlimitedWorkerLimit, formatWorkerCapacity } = helpers;

const CASES = [];
function record(name, fn) { CASES.push({ name, fn }); }

record('U1-U4 pure helper cases', () => {
// U1 sentinel
const u1 = formatWorkerCapacity(3, 1e15, 1e15 - 3);
assert.strictEqual(u1.used, '3 workers', 'U1 used text');
assert.strictEqual(u1.limit, 'Unlimited', 'U1 limit text');
assert.strictEqual(u1.available, 'Unlimited', 'U1 available text');
assert.strictEqual(u1.limitVisible, false, 'U1 limit hidden');

// U2 finite keeps numbers and "used / limit" separator visible
const u2 = formatWorkerCapacity(3, 8, 5);
assert.strictEqual(u2.used, 3, 'U2 used raw');
assert.strictEqual(u2.limit, 8, 'U2 limit raw');
assert.strictEqual(u2.available, 5, 'U2 available raw');
assert.strictEqual(u2.limitVisible, true, 'U2 limit visible');
assert.strictEqual(`${u2.used} / ${u2.limit}`, '3 / 8', 'U2 composed text');

// U3 null / undefined limit -> Unlimited
assert.strictEqual(isUnlimitedWorkerLimit(null), true, 'U3 null');
assert.strictEqual(isUnlimitedWorkerLimit(undefined), true, 'U3 undefined');
assert.strictEqual(formatWorkerCapacity(2, null, null).limit, 'Unlimited', 'U3 null limit text');

// U4 threshold edge
assert.strictEqual(isUnlimitedWorkerLimit(1e12), true, 'U4 at threshold');
assert.strictEqual(isUnlimitedWorkerLimit(1e12 - 1), false, 'U4 below threshold');
assert.strictEqual(isUnlimitedWorkerLimit(0), false, 'U4 zero is a finite cap');
});

// --- U5/U6 loader against a tiny DOM ---------------------------------------
function makeDom(ids) {
    const els = {};
    const prevSibling = {};
    ids.forEach((id, i) => {
        els[id] = { id, textContent: '-', style: {}, previousElementSibling: null };
    });
    // separator sits just before #max-workers-count
    els['max-workers-count'].previousElementSibling = { style: {} };
    return {
        els,
        getElementById: (id) => els[id] || null,
    };
}
const IDS = ['licensed-count', 'max-workers-count', 'available-count',
    'unlicensed-count', 'offline-count'];

async function runLoader(maxWorkers, available) {
    const dom = makeDom(IDS);
    const loadLicenseSummary = new Function(
        'document', 'window', 'sessionStorage', 'fetch', 'console', 'showError',
        helperBlock + '\n' + loaderBlock + '\nreturn loadLicenseSummary;'
    )(dom, { APP_CONFIG: { API_URL: 'http://api.test' } },
        { getItem: () => 'tok' },
        async () => ({
            status: 200, ok: true,
            json: async () => ({
                workers: { licensed: 3, available_licenses: available, unlicensed: 1, offline: 0 },
                license: { max_workers: maxWorkers, expires_at: null },
            }),
        }),
        { log() {}, warn() {}, error() {} },
        () => {});
    await loadLicenseSummary();
    return dom;
}

record('U5 CE sentinel renders Unlimited', async () => {
    const dom = await runLoader(1e15, 1e15 - 3);
    assert.strictEqual(dom.els['licensed-count'].textContent, '3 workers');
    assert.strictEqual(dom.els['max-workers-count'].textContent, 'Unlimited');
    assert.strictEqual(dom.els['available-count'].textContent, 'Unlimited');
    assert.strictEqual(dom.els['max-workers-count'].style.display, 'none');
    assert.strictEqual(dom.els['max-workers-count'].previousElementSibling.style.display, 'none');
    assert.strictEqual(dom.els['unlicensed-count'].textContent, 1);
});

record('U6 finite limit renders used / limit unchanged', async () => {
    const dom = await runLoader(8, 5);
    assert.strictEqual(dom.els['licensed-count'].textContent, 3);
    assert.strictEqual(dom.els['max-workers-count'].textContent, 8);
    assert.strictEqual(dom.els['available-count'].textContent, 5);
    assert.strictEqual(dom.els['max-workers-count'].style.display, '');
    assert.strictEqual(dom.els['max-workers-count'].previousElementSibling.style.display, '');
});

// U7 shipped source markers
record('U7 source carries threshold and block markers', async () => {
    assert.ok(src.includes('const WORKER_LIMIT_UNLIMITED_THRESHOLD = 1e12;'));
    assert.ok(src.includes('// FIX-WORKERS-UNLIMITED-DISPLAY END'));
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

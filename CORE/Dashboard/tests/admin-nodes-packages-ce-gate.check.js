// Node-runnable check for the CE edition gate on the Packages tab of the
// platform-admin Catalog page (Dashboard/js/admin-nodes.js, rc20 class b:
// "CE frontend calls a CE-absent endpoint"). On CE, routers/nodes_marketplace.py
// is api_deny (open-core/manifest.yaml:1586), so the page's init fetch
// GET /v1/nodes/packages 404s and the e2e net-guard flags it
// (admin-catalog-stacks.spec.ts:210 "404 response(s) for a path under /v1/").
//
// Same technique as analytics-burst-ce-gate.check.js / settings-scheduler-subtab-init.check.js:
// the gate code is EXTRACTED VERBATIM from the shipped source text and run
// against a stub DOM, so the assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/admin-nodes-packages-ce-gate.check.js
//
// Asserts: CE -> loadPackages() never fetches nodes/packages and shows the
// page's existing "nothing here" state; paid/unknown edition -> the fetch
// still happens (Pro behaviour unchanged).
'use strict';
const fs = require('fs');
const path = require('path');

const nodesSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'admin-nodes.js'), 'utf8');
const apiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');

// Brace-matched block whose '{' is at `open`.
function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}

const PKG_HDR = 'async function loadPackages()';
const pkgAt = nodesSrc.indexOf(PKG_HDR);
if (pkgAt < 0) throw new Error('loadPackages not found');
const loadPackagesSrc = `${PKG_HDR} ${blockAt(nodesSrc, nodesSrc.indexOf('{', pkgAt + PKG_HDR.length))}`;

// The gate must sit BEFORE the first nodes/packages fetch inside loadPackages
// (old-file state has no gate -> throw -> non-zero exit = FAIL).
const GATE_NEEDLE = 'if (!await isPaidEdition()) {';
const gateAt = loadPackagesSrc.indexOf(GATE_NEEDLE);
if (gateAt < 0) throw new Error('CE gate not found in loadPackages (old-file state)');
const fetchAt = loadPackagesSrc.indexOf('`${apiUrl}/nodes/packages`');
if (!(fetchAt > gateAt)) throw new Error('gate not before fetch');

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

// --- stub DOM (mirrors analytics-burst-ce-gate.check.js) ---------------------
// Initial classes match pages/admin-nodes.html: loading visible ("Loading
// packages..."), empty + list start hidden.
function makeDom() {
    const seed = {
        'admin-packages-loading': [],
        'admin-packages-empty': ['hidden'],
        'admin-packages-list': ['hidden'],
    };
    const els = {};
    const doc = {
        getElementById: (id) => {
            const e = els[id] || (els[id] = {
                classes: new Set(seed[id] || []),
                style: { setProperty: () => {} },
                dataset: {},
                addEventListener: () => {},
                display: null,
            });
            e.classList = {
                remove: (c) => e.classes.delete(c),
                add: (c) => e.classes.add(c),
                toggle: (c) => e.classes.has(c) ? e.classes.delete(c) : e.classes.add(c),
            };
            return e;
        },
    };
    const visible = (id) => { const e = els[id]; return !!e && e.display !== 'none' && !e.classes.has('hidden'); };
    return { doc, visible };
}

// Run the shipped loadPackages against the stub DOM. The source awaits
// isPaidEdition(), so the run is awaited (the sync promise-like stub resolves
// inline); fetch is stubbed to count calls.
async function runLoadPackages(paid, pkgs) {
    const dom = makeDom();
    let fetched = 0;
    const resp = { ok: true, json: () => pkgs, text: () => 'HTTP 404' };
    const fn = new Function('document', 'isPaidEdition', 'fetch', '_hdr', 'apiUrl', 'escapeHtml', '_dollars',
        `${loadPackagesSrc}\nreturn loadPackages();`);
    await fn(
        dom.doc,
        () => ({ then: (cb) => cb(paid) }),
        (url, opts) => { fetched++; return resp; },
        () => ({ 'Authorization': 'Bearer x', 'Content-Type': 'application/json' }),
        'http://api.localhost',
        (s) => String(s),
        (cents) => cents ? `$${(cents / 100).toFixed(2)}` : '—');
    return { fetched, dom };
}

(async function () {
    const ce = await runLoadPackages(false, []);
    check('CE: loadPackages does NOT fetch nodes/packages (no 404 under /v1/)', ce.fetched === 0);
    check('CE: loading state cleared', !ce.dom.visible('admin-packages-loading'));
    check('CE: existing empty state shown ("No packages yet.")', ce.dom.visible('admin-packages-empty'));
    check('CE: packages list stays hidden', !ce.dom.visible('admin-packages-list'));

    const paid = await runLoadPackages(true, []);
    check('paid: fetch still happens (Pro behaviour unchanged)', paid.fetched === 1);
    check('paid: empty-state path intact', paid.dom.visible('admin-packages-empty'));

    const paidRows = await runLoadPackages(true, [{ id: 1, name: 'P', price_cents: 100 }]);
    check('paid + rows: fetch happens before the list render', paidRows.fetched === 1);

    // --- helper reuse (no invented gate) ------------------------------------
    check('admin-nodes.js imports the existing isPaidEdition from ./api.js',
        /import\s*\{\s*isPaidEdition\s*\}\s*from\s*'\.\/api\.js';/.test(nodesSrc));
    check('api.js exports isPaidEdition (the SAME helper admin-plans.js:18 uses)',
        /export async function isPaidEdition\(\)/.test(apiSrc));
    check('no new edition helper invented in admin-nodes.js',
        !/function\s+_?ceEdition|const\s+_ceEdition/.test(nodesSrc));
    // FAILOPEN-C (working tree): api.js isPaidEdition is now fail-CLOSED - only a
    // positive saas/pro edition counts as paid; unknown/error/throw -> false. The
    // old check pinned `/return true;/` inside the helper, which the new body no
    // longer contains. Pin the NEW shape instead: the positive edition test, the
    // unknown->false guard, and NO unconditional `return true` anywhere in the
    // helper body (a bare `return true;` would be fail-open).
    const paidHdrAt = apiSrc.indexOf('export async function isPaidEdition');
    if (paidHdrAt < 0) throw new Error('isPaidEdition not exported in api.js');
    const paidHelper = `export async function isPaidEdition ${blockAt(apiSrc, apiSrc.indexOf('{', paidHdrAt + 'export async function isPaidEdition'.length))}`;
    check('api.js isPaidEdition is fail-closed (positive saas/pro test)',
        /ent\.edition === 'saas' \|\| ent\.edition === 'pro'/.test(paidHelper));
    check('api.js isPaidEdition treats unknown/error as CE (if (!ent || ent.error) return false)',
        /if \(!ent \|\| ent\.error\) return false;/.test(paidHelper));
    check('api.js isPaidEdition has no unconditional return true (fail-open shape gone)',
        !/return true;/.test(paidHelper));

    // --- the fetch sites that remain ----------------------------------------
    const pkgFetches = nodesSrc.split('\n').filter((l) => l.includes('`${apiUrl}/nodes/packages`'));
    check('exactly 2 nodes/packages sites remain (init GET + create POST)', pkgFetches.length === 2);
    check('the init GET is the guarded one (inside loadPackages, after the gate)',
        loadPackagesSrc.includes('`${apiUrl}/nodes/packages`'));

    // --- no new unescaped innerHTML in the gate ------------------------------
    const gateBlock = blockAt(loadPackagesSrc, loadPackagesSrc.indexOf('{', gateAt));
    check('CE gate block touches only classList state (no innerHTML, no fetch)',
        !/innerHTML|fetch\(/.test(gateBlock));

    if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
    console.log('\nall checks passed');
})();

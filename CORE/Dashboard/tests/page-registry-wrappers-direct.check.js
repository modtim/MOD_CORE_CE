'use strict';
// FIX-CE-NOPAID-4-2-LEG-B3-GAP — DIRECT test of the ui.js WRAPPERS.
//
// The four leg-B3 lift tests (ce-dead-nav-3 / drift-events-ce-retired /
// overage-caps-ce-retired / test_page_registry.js) lift the REAL text of
// _ceDeniedPageId and _ceRetiredAdminGate, but they REPLACE the CE-NOPAID-4.2
// wrappers with a test-local stand-in (`const getPageEntry = (id) =>
// _ceRegisteredRows()[id];`), so the shipped wrapper bodies in js/ui.js are
// never executed. This check lifts the REAL text of getPageEntry and isPaidPage
// out of js/ui.js (slice BY FUNCTION NAME, not by line number) and runs them,
// so a mutation inside either body turns this file red.
//
// Only the ONE registry seam (_ceRegisteredRows, ui-manifest.js, tested by
// test_page_registry.check.js) is stood in; the wrappers themselves are shipped
// source. Nothing here edits ui.js.
//
//   node CORE/Dashboard/tests/page-registry-wrappers-direct.check.js
//   (optional 2nd arg: an alternate ui.js path — used only for the mutation
//    proof, see the result file)

const fs = require('fs');
const path = require('path');
const assert = require('assert');
const test = require('node:test');

const ROOT = path.join(__dirname, '..');
const uiSrc = fs.readFileSync(process.argv[2] || path.join(ROOT, 'js', 'ui.js'), 'utf8');

function liftFn(header) {
    const i = uiSrc.indexOf(header);
    if (i < 0) throw new Error('could not lift ' + header + ' from ui.js');
    let open = uiSrc.indexOf('{', i);
    let depth = 0;
    for (let k = open; k < uiSrc.length; k++) {
        if (uiSrc[k] === '{') depth++;
        else if (uiSrc[k] === '}') { depth--; if (!depth) return uiSrc.slice(i, k + 1); }
    }
    throw new Error('unbalanced braces at ' + header);
}

const entryFn = liftFn('function getPageEntry(pageId) {');
const paidFn = liftFn('function isPaidPage(pageId) {');

// The seam: id -> row, the same shape ui-manifest.js._ceRegisteredRows() returns.
const ROWS = {
    'admin-nodes': { id: 'admin-nodes', gate: 'retired-admin', paid: true },
    'marketplace': { id: 'marketplace', gate: 'paid-hub', paid: true },
    'settings': { id: 'settings', gate: null, paid: false },
};

const src = 'const _ceRegisteredRows = () => (' + JSON.stringify(ROWS) + ');\n'
    + entryFn + '\n' + paidFn + '\n'
    + 'return { getPageEntry, isPaidPage };\n';
const w = new Function(src)();

test('wrappers: getPageEntry of a registered page returns THAT row', () => {
    const e = w.getPageEntry('admin-nodes');
    assert.strictEqual(e.id, 'admin-nodes');
    assert.strictEqual(e.gate, 'retired-admin');
    assert.strictEqual(e.paid, true);
});

test('wrappers: getPageEntry of an unknown page is undefined (documented fallback)', () => {
    assert.strictEqual(w.getPageEntry('no-such-page'), undefined);
});

test('wrappers: isPaidPage is true for a paid-tagged row', () => {
    assert.strictEqual(w.isPaidPage('admin-nodes'), true);
});

test('wrappers: isPaidPage is false for a CE page (paid:false)', () => {
    assert.strictEqual(w.isPaidPage('settings'), false);
});

test('wrappers: isPaidPage is false/safe for an unknown page', () => {
    assert.strictEqual(w.isPaidPage('no-such-page'), false);
});

test('wrappers: isPaidPage is true for marketplace (no ceReachable carve-out 11-16; the deny lives in _ceDeniedPageId)', () => {
    assert.strictEqual(w.isPaidPage('marketplace'), true);
});

test('wrappers: the lifted bodies are the shipped ui.js text', () => {
    assert.ok(uiSrc.includes(entryFn) && uiSrc.includes(paidFn));
    assert.ok(entryFn.includes('_ceRegisteredRows()[pageId]'));
    assert.ok(paidFn.includes('getPageEntry(pageId)'));
});

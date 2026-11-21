//  — jsdom proof for the
// main-row locator shared by _psRenderEditDefinition (defOrigin stamp) and
// _psSeedResultCell (ps-seed-result span) in js/persistent-stacks.js.
//
// The panel slot ps-backups-<id> is a <tr> inside <tbody id="ps-rows">, so the
// old walk host.parentElement.previousElementSibling is the TBODY's previous
// sibling (a <thead>, or null when the panel row is first) — never the stack's
// main row. So mainRow.dataset.defOrigin was never recorded on the real main
// row, and the auto-refresh patcher could clobber a definition the operator
// edited. The fix resolves the main row by the stack id the slot's own id
// carries (data-ps-main / data-stack-id, stamped by _psStackRow:3302) via the
// single helper _psMainRowOf, which both call sites use.
//
// Asserts:
//   (a) real table shape (thead + main row + backups/panel row): the render
//       path stamps data-def-origin on the MAIN row, never on the panel row
//       or the thead;
//   (b) panel row FIRST in the tbody (the old walk hit the thead): defOrigin
//       still lands on the main row, thead untouched;
//   (c) two stacks with the DOM's previous row belonging to another stack
//       (patcher re-order at :3430): defOrigin lands on the PAIRED main row;
//   (d) no matching main row: no stamp, no crash;
//   (e) the helper and _psSeedResultCell agree on the same main row (one
//       locator, no second copy);
//   (f) the D8 seed-result proof still holds through the shared helper.
//
//   node --test CORE/Dashboard/tests/persistent-stacks-edit-definition-main-row.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

function slice(a, b) {
    const start = SRC.indexOf(a);
    const end = SRC.indexOf(b);
    assert.ok(start >= 0 && end > start, `locate ${a} .. ${b}`);
    return SRC.slice(start, end);
}

// The shared locator plus the seed-result cell that now uses it.
const SEED_BLOCK = slice('function _psMainRowOf(host) {', 'function _psSeedText(host, testid, text) {');
// The real edit-definition render path (the defOrigin stamp lives here).
const RENDER_BLOCK = slice('function _psRenderEditDefinition(id) {', 'async function _psEditDefinition(id) {');

function loadJSDOM() {
    const candidates = [
        path.join(__dirname, '..', '..', 'WorkflowBuilder', 'node_modules', 'jsdom'),
        path.join(__dirname, '..', '..', '..', 'WorkflowBuilder', 'node_modules', 'jsdom'),
    ];
    let last;
    for (const p of candidates) {
        try { return require(p); } catch (e) { last = e; }
    }
    throw last;
}
const JSDOM = loadJSDOM().JSDOM;

function loadFns(document, id, state) {
    const sandbox = {
        console: { log: () => {}, warn: () => {}, error: () => {} },
        document,
        Number: globalThis.Number, String: globalThis.String, Object: globalThis.Object,
        // Stubs for the render path's collaborators (none of them move the row).
        _psEsc: (v) => String(v),
        _psDefRevisionOptions: () => '',
        _psHaClassControl: () => '',
        _psMountDefVariables: () => {},
        _psRenderSecretsSection: () => {},
        // :
        // _psRenderEditDefinition now also calls the CE render-preview gate
        // (js/persistent-stacks.js:930); this sandbox stubs every other
        // collaborator, so stub it too — the preview button is irrelevant to
        // this test's defOrigin assertions. Pre-existing red, one-line fix.
        _psCeRenderPreviewHidden: () => false,
        _psDefVarsCtrls: {},
        _psDefState: { [id]: { compose_yaml: 'compose: yaml-text', compose_spec: {} } },
    };
    return vm.runInNewContext(
        `"use strict";\n${SEED_BLOCK}\n${RENDER_BLOCK}\n({ _psMainRowOf, _psSeedResultCell, _psRenderEditDefinition });`,
        sandbox, { filename: 'ps-edit-definition-main-row' });
}

function mainRowHtml(id) {
    return `<tr data-ps-main="${id}" data-stack-id="${id}" data-ps-sig="s${id}"><td class="px-4 py-2">${id}</td><td>name ${id}</td></tr>`;
}
function panelHtml(id) {
    return `<tr id="ps-backups-${id}"><td colspan="8">panel ${id}</td></tr>`;
}
function build(bodyHtml) {
    const dom = new JSDOM(`<!DOCTYPE html><html><body><table><thead><tr><th>Stack</th></tr></thead><tbody id="ps-rows">${bodyHtml}</tbody></table></body></html>`);
    const document = dom.window.document;
    return { document, tbody: document.getElementById('ps-rows') };
}
function stampedIn(scope) {
    return scope.querySelectorAll('[data-def-origin]');
}

test('a: render path stamps defOrigin on the paired main row', () => {
    const { document, tbody } = build(mainRowHtml(7) + panelHtml(7));
    const fns = loadFns(document, 7);
    const host = document.getElementById('ps-backups-7');
    assert.ok(host && host.id === 'ps-backups-7', 'slot is the panel row');
    fns._psRenderEditDefinition(7);
    const main = tbody.querySelector('tr[data-ps-main="7"]');
    assert.strictEqual(main.dataset.defOrigin !== undefined, true, 'main row carries defOrigin');
    assert.strictEqual(main.dataset.defOrigin, 'compose: yaml-text', 'defOrigin is the rendered YAML');
    assert.ok(host.innerHTML.indexOf('ps-def-compose-7') >= 0, 'textarea rendered in the panel row');
    assert.strictEqual(stampedIn(host).length, 0, 'panel row never carries defOrigin');
    const thead = document.querySelector('thead');
    assert.strictEqual(stampedIn(thead).length, 0, 'thead never carries defOrigin');
});

test('b: panel row first in the tbody (the old walk hit the thead)', () => {
    const { document, tbody } = build(panelHtml(7) + mainRowHtml(7));
    const fns = loadFns(document, 7);
    fns._psRenderEditDefinition(7);
    const main = tbody.querySelector('tr[data-ps-main="7"]');
    assert.strictEqual(main.dataset.defOrigin !== undefined, true, 'main row stamped even when the panel row is first');
    const thead = document.querySelector('thead');
    assert.strictEqual(stampedIn(thead).length, 0, 'thead untouched');
    assert.strictEqual(document.querySelectorAll('[data-def-origin]').length, 1, 'exactly one stamped row');
});

test('c: two stacks, panel of 9 next to main of 7 (patcher re-order)', () => {
    const { document, tbody } = build(mainRowHtml(7) + panelHtml(9) + mainRowHtml(9));
    const fns = loadFns(document, 9);
    fns._psRenderEditDefinition(9);
    const main9 = tbody.querySelector('tr[data-ps-main="9"]');
    const main7 = tbody.querySelector('tr[data-ps-main="7"]');
    assert.strictEqual(main9.dataset.defOrigin !== undefined, true, 'stack 9 main row stamped');
    assert.strictEqual(main7.dataset.defOrigin !== undefined, false, 'stack 7 main row untouched');
});

test('d: no matching main row -> no stamp, no crash', () => {
    const { document, tbody } = build(panelHtml(7));
    const fns = loadFns(document, 7);
    fns._psRenderEditDefinition(7);
    assert.strictEqual(document.querySelectorAll('[data-def-origin]').length, 0, 'nothing stamped');
    assert.strictEqual(fns._psMainRowOf(document.getElementById('ps-backups-7')), null, 'helper returns null');
});

test('e: one locator — helper and seed-result cell agree on the same row', () => {
    const { document, tbody } = build(mainRowHtml(7) + panelHtml(7));
    const fns = loadFns(document, 7);
    const host = document.getElementById('ps-backups-7');
    const viaHelper = fns._psMainRowOf(host);
    const span = fns._psSeedResultCell(host);
    assert.ok(span, 'seed span created');
    const main = tbody.querySelector('tr[data-ps-main="7"]');
    assert.strictEqual(viaHelper, main, 'helper resolves the main row');
    assert.strictEqual(main.querySelectorAll('[data-testid="ps-seed-result"]').length, 1, 'span lives in that same row');
    assert.strictEqual(host.querySelectorAll('[data-testid="ps-seed-result"]').length, 0, 'not in the panel row');
});

test('f: the D8 seed-result proof still holds through the shared helper', () => {
    const { document, tbody } = build(panelHtml(7) + mainRowHtml(7));
    const fns = loadFns(document, 7);
    const host = document.getElementById('ps-backups-7');
    const span = fns._psSeedResultCell(host);
    assert.ok(span, 'span still created when the panel row is first');
    const main = tbody.querySelector('tr[data-ps-main="7"]');
    assert.strictEqual(main.querySelectorAll('[data-testid="ps-seed-result"]').length, 1, 'span in the main row');
    const thead = document.querySelector('thead');
    assert.strictEqual(thead.querySelectorAll('[data-testid="ps-seed-result"]').length, 0, 'never in the thead');
});

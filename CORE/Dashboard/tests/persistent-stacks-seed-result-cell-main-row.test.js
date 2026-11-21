// FIX-DASH-PS-SEED-RESULT-CELL-MAIN-ROW-D8 — jsdom proof for the seed result
// line locator in js/persistent-stacks.js (_psSeedResultCell).
//
// _psSeedSlot(id) returns the PANEL row (tr id="ps-backups-<id>"), so the old
// locator walked host.parentElement.previousElementSibling — that is the TBODY's
// previous sibling (a <thead>, or null), never the stack's main row, so the
// ps-seed-result span was never created and the seed result line stayed
// invisible. The fix resolves the main row by the stack id the slot carries
// (data-ps-main / data-stack-id, stamped by _psStackRow:3302).
//
// Asserts:
//   (a) normal order (main row, then panel row): the span lands in the MAIN row;
//   (b) panel row first in the tbody with a <thead> (the e2e Stacks table): the
//       span still lands in the main row, never in the thead;
//   (c) two stacks: the panel of stack 9 resolves stack 9's main row even when
//       the DOM's previous row belongs to stack 7 (patcher re-order at :3430);
//   (d) idempotent: an existing ps-seed-result span is reused, not duplicated;
//   (e) no matching main row -> null (no crash, no stray span).
//
//   node --test CORE/Dashboard/tests/persistent-stacks-seed-result-cell-main-row.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');
const start = SRC.indexOf('function _psSeedSlot(id) {');
const end = SRC.indexOf('function _psSeedText(host, testid, text) {');
assert.ok(start >= 0 && end > start, 'locate the seed-slot/result-cell block');
const block = SRC.slice(start, end);

// jsdom lives in CORE/WorkflowBuilder/node_modules. Under `node --test tests/...`
// from the harness (r9700-run.sh nodetest Dashboard ...) /app is CORE/Dashboard, so
// that dir is mounted at the container root (/WorkflowBuilder/node_modules); from a
// repo-root run it is two levels up. Try both.
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

function seedFns(document) {
    const sandbox = {
        console: { log: () => {}, warn: () => {}, error: () => {} },
        document,
        Number: globalThis.Number, String: globalThis.String, Object: globalThis.Object,
    };
    return vm.runInNewContext(
        `"use strict";\n${block}\n({ _psSeedSlot, _psSeedResultCell, _psSeedResultText });`,
        sandbox, { filename: 'ps-seed-result-cell' });
}

function mainRowHtml(id) {
    return `<tr data-ps-main="${id}" data-stack-id="${id}" data-ps-sig="s${id}"><td class="px-4 py-2">${id}</td><td>name ${id}</td></tr>`;
}
function panelHtml(id) {
    return `<tr id="ps-backups-${id}"><td colspan="8">panel ${id}</td></tr>`;
}
function build(bodyHtml) {
    const dom = new JSDOM(`<!DOCTYPE html><html><body><table><thead><tr><th>Stack</th></tr></thead><tbody id="ps-rows">${bodyHtml}</tbody></table></body></html>`);
    return { document: dom.window.document, tbody: dom.window.document.getElementById('ps-rows') };
}
function spansIn(row) {
    return row.querySelectorAll('[data-testid="ps-seed-result"]');
}

test('a: main row then panel row', () => {
    const { document, tbody } = build(mainRowHtml(7) + panelHtml(7));
    const fns = seedFns(document);
    const host = fns._psSeedSlot(7);
    assert.ok(host && host.id === 'ps-backups-7', 'slot is the panel row');
    const span = fns._psSeedResultCell(host);
    assert.ok(span, 'result span created');
    const main = tbody.querySelector('tr[data-ps-main="7"]');
    assert.strictEqual(spansIn(main).length, 1, 'span lives in the main row');
    assert.strictEqual(spansIn(host).length, 0, 'span not in the panel row');
    fns._psSeedResultText(host, 'Seed uploaded.');
    assert.strictEqual(span.textContent, 'Seed uploaded.', 'text written to that span');
});

test('b: panel row first in the tbody (thead is the old wrong target)', () => {
    const { document, tbody } = build(panelHtml(7) + mainRowHtml(7));
    const fns = seedFns(document);
    const host = fns._psSeedSlot(7);
    const span = fns._psSeedResultCell(host);
    assert.ok(span, 'result span still created when the panel row is first');
    const main = tbody.querySelector('tr[data-ps-main="7"]');
    assert.strictEqual(spansIn(main).length, 1, 'span lives in the main row');
    const thead = document.querySelector('thead');
    assert.strictEqual(thead.querySelectorAll('[data-testid="ps-seed-result"]').length, 0, 'never in the thead');
});

test('c: two stacks, panel of 9 next to main of 7', () => {
    const { document, tbody } = build(mainRowHtml(7) + panelHtml(7) + mainRowHtml(9) + panelHtml(9));
    const fns = seedFns(document);
    const host = fns._psSeedSlot(9);
    const span = fns._psSeedResultCell(host);
    assert.ok(span, 'result span created for stack 9');
    assert.strictEqual(spansIn(tbody.querySelector('tr[data-ps-main="9"]')).length, 1, 'in stack 9 main row');
    assert.strictEqual(spansIn(tbody.querySelector('tr[data-ps-main="7"]')).length, 0, 'not in stack 7 main row');
});

test('d: idempotent reuse', () => {
    const { document, tbody } = build(mainRowHtml(7) + panelHtml(7));
    const fns = seedFns(document);
    const host = fns._psSeedSlot(7);
    const first = fns._psSeedResultCell(host);
    const second = fns._psSeedResultCell(host);
    assert.strictEqual(first, second, 'same span returned');
    assert.strictEqual(spansIn(tbody.querySelector('tr[data-ps-main="7"]')).length, 1, 'exactly one span');
});

test('e: no matching main row -> null', () => {
    const { document, tbody } = build(panelHtml(7));
    const fns = seedFns(document);
    const host = fns._psSeedSlot(7);
    assert.strictEqual(fns._psSeedResultCell(host), null, 'null, no crash');
    assert.strictEqual(document.querySelectorAll('[data-testid="ps-seed-result"]').length, 0, 'no stray span');
});

// FIX-DASH-WIDGETS-UNTESTED-MODULES-JEST-TESTS — CORE/Dashboard/js/bulk-skip-modal.js
// (data-files-launch-bulletproof §2: bulk-operation skip toast + skipped-items modal).
// The module is an ES module, so this test extracts its body (from `const REASON_HUMAN_COPY`
// to EOF, `export function` -> `function`) and runs it in a vm sandbox whose `document` is
// a small DOM registry (createElement registers an element when the module assigns `.id`,
// getElementById resolves it, remove() detaches it), then asserts behaviour:
//   (a) reasonToHuman: the reason -> human copy table is the single source of truth,
//       {verb} substitution (default 'use'), unknown reason falls back to 'unknown' copy;
//   (b) pastTense: closed verb set, unknown verb -> verb + 'd', empty -> 'completed';
//   (c) showBulkSkipWarning: no-op on empty/non-array, one toast appended, past-tense copy,
//       "See the list (N)" count, handlers wired, 10s auto-dismiss scheduled and honoured;
//   (d) renderBulkSkipModal: one <li> per skipped item, heading count, name fallback
//       `Item #<id>`, reason copy, hostile name HTML-escaped (no raw <img survives);
//   (e) Escape closes the modal (a non-Escape key does not), outside-click closes it,
//       close/done buttons wired, re-render tears down the previous modal (no stacking).
//
//   node --test CORE/Dashboard/tests/bulk-skip-modal.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'bulk-skip-modal.js'), 'utf8');
const start = src.indexOf('const REASON_HUMAN_COPY');
assert.ok(start >= 0, 'could not locate the bulk-skip-modal body');
const block = src.slice(start).replace(/export function/g, 'function');

function run() {
    const registry = {};
    const body = { children: [] };
    const timers = [];
    const listeners = {};

    function mkNode(id) {
        const node = { className: '', value: '', disabled: false, innerHTML: '', textContent: '', dataset: {} };
        let cur = id || '';
        Object.defineProperty(node, 'id', {
            get: () => cur,
            set: (v) => { cur = v; if (v) registry[v] = node; },
        });
        if (cur) registry[cur] = node;
        node.listeners = {};
        node.addEventListener = (type, fn) => { node.listeners[type] = fn; listeners[`${cur}:${type}`] = fn; };
        node.removeEventListener = () => {};
        node.appendChild = (child) => { (node.children || (node.children = [])).push(child); };
        node.classList = { add: () => {}, remove: () => {} };
        node.querySelector = () => null;
        node.querySelectorAll = () => [];
        node.remove = () => {
            const i = body.children.indexOf(node);
            if (i >= 0) body.children.splice(i, 1);
            if (cur) delete registry[cur];
        };
        return node;
    }

    const sandbox = {
        console: { log: () => {}, warn: () => {}, error: () => {} },
        setTimeout: (fn, ms) => { timers.push({ fn, ms }); },
        document: {
            getElementById: (id) => registry[id] || (registry[id] = mkNode(id)),
            createElement: (tag) => mkNode(tag),
            querySelectorAll: () => [],
            querySelector: () => null,
            addEventListener: (type, fn) => { listeners[`document:${type}`] = fn; },
            removeEventListener: () => {},
            body: {
                appendChild: (n) => { body.children.push(n); },
                contains: (n) => body.children.includes(n),
            },
        },
    };
    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ showBulkSkipWarning, renderBulkSkipModal, reasonToHuman, pastTense, escapeHtml });`,
        sandbox, { filename: 'bulk-skip-modal-body' });
    return { fns, body, timers, listeners };
}

test('reasonToHuman: copy table, {verb} substitution, unknown fallback', () => {
    const r = run();
    assert.strictEqual(r.fns.reasonToHuman('not_owner', 'delete'),
        "You're not the owner of this file");
    assert.strictEqual(r.fns.reasonToHuman('no_permission', 'move'),
        "You don't have permission to move this file");
    assert.strictEqual(r.fns.reasonToHuman('no_permission'),
        "You don't have permission to use this file", '{verb} default must be use');
    assert.strictEqual(r.fns.reasonToHuman('system_generated', 'delete'),
        "This is a system folder and can't be modified");
    assert.strictEqual(r.fns.reasonToHuman('audit_locked', 'delete'),
        'Locked for audit (workflow run output)');
    assert.strictEqual(r.fns.reasonToHuman('name_collision', 'copy'),
        'A file with this name already exists in the destination');
    assert.strictEqual(r.fns.reasonToHuman('quota_exceeded', 'upload'),
        'Storage is full — free up space to continue.');
    assert.strictEqual(r.fns.reasonToHuman('made_up_reason', 'share'),
        'Could not complete this operation', 'unknown reason must fall back');
});

test('pastTense: closed verb set, unknown -> verb+d, empty -> completed', () => {
    const r = run();
    assert.strictEqual(r.fns.pastTense(''), 'completed');
    assert.strictEqual(r.fns.pastTense(null), 'completed');
    assert.strictEqual(r.fns.pastTense('Delete'), 'deleted');
    assert.strictEqual(r.fns.pastTense(' moved '), 'moved');
    assert.strictEqual(r.fns.pastTense('copied'), 'copied');
    assert.strictEqual(r.fns.pastTense('rename'), 'renamed');
    assert.strictEqual(r.fns.pastTense('download'), 'downloaded');
    assert.strictEqual(r.fns.pastTense('upload'), 'uploaded');
    assert.strictEqual(r.fns.pastTense('archive'), 'archived', 'unknown verb gets -d');
});

test('showBulkSkipWarning: empty input no-ops, toast copy + count + handlers + 10s dismiss', () => {
    const r = run();
    r.fns.showBulkSkipWarning({ verb: 'delete', skippedItems: [] });
    assert.strictEqual(r.body.children.length, 0, 'toast rendered for an empty skip list');
    r.fns.showBulkSkipWarning({ verb: 'delete', skippedItems: 'not-an-array' });
    assert.strictEqual(r.body.children.length, 0, 'toast rendered for a non-array');

    const items = [{ id: 1, name: 'a.txt', reason: 'not_owner' },
        { id: 2, name: 'b.txt', reason: 'no_permission' }];
    r.fns.showBulkSkipWarning({ verb: 'delete', skippedItems: items });
    assert.strictEqual(r.body.children.length, 1, 'exactly one toast must be appended');
    const toast = r.body.children[0];
    assert.strictEqual(toast.id, 'bulk-skip-toast', 'toast id missing');
    assert.ok(/Not all files could be deleted/.test(toast.innerHTML), 'past-tense copy missing');
    assert.ok(/See the list \(2\)/.test(toast.innerHTML), 'count copy missing');
    assert.ok(r.listeners['bulk-skip-see-list:click'], 'see-list handler not wired');
    assert.ok(r.listeners['bulk-skip-toast-close:click'], 'dismiss handler not wired');
    assert.strictEqual(r.timers.length, 1, 'auto-dismiss timer not scheduled');
    assert.strictEqual(r.timers[0].ms, 10000, 'auto-dismiss must be 10s');
    r.timers[0].fn();
    assert.strictEqual(r.body.children.length, 0, 'auto-dismiss must remove the toast');
});

test('renderBulkSkipModal: rows per item, name fallback, hostile name escaped', () => {
    const r = run();
    const items = [
        { id: 7, name: 'quarterly-report.pdf', reason: 'audit_locked' },
        { id: 8, reason: 'not_found' },
        { id: 9, name: '<img src=x onerror=alert(1)>', reason: 'no_permission' },
    ];
    r.fns.renderBulkSkipModal({ verb: 'move', skippedItems: items });
    const modal = r.body.children[0];
    assert.strictEqual(modal.id, 'bulk-skip-modal', 'modal id missing');
    const html = modal.innerHTML;
    assert.strictEqual((html.match(/<li /g) || []).length, 3, 'one <li> per skipped item');
    assert.ok(html.includes('Could not be moved (3)'), 'heading count missing');
    assert.ok(html.includes('Already deleted or moved'), 'not_found copy missing');
    assert.ok(html.includes('Item #8'), 'name fallback missing');
    assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'), 'hostile name must be escaped');
    assert.ok(!/<img src=x/.test(html), 'raw hostile markup survived into the modal');
    assert.ok(r.listeners['bulk-skip-modal-close:click'], 'close handler not wired');
    assert.ok(r.listeners['bulk-skip-modal-done:click'], 'done handler not wired');

    // Escape closes; a non-Escape key does not.
    r.listeners['document:keydown']({ key: 'a' });
    assert.ok(r.body.children.includes(modal), 'non-Escape key must not close');
    r.listeners['document:keydown']({ key: 'Escape' });
    assert.ok(!r.body.children.includes(modal), 'Escape must close the modal');

    // Outside-click closes.
    r.fns.renderBulkSkipModal({ verb: 'move', skippedItems: items });
    const modal2 = r.body.children[r.body.children.length - 1];
    r.listeners['bulk-skip-modal:click']({ target: modal2 });
    assert.ok(!r.body.children.includes(modal2), 'outside click must close the modal');
});

test('renderBulkSkipModal: re-render tears down the previous modal (no stacking)', () => {
    const r = run();
    const items = [{ id: 1, name: 'a', reason: 'unknown' }];
    r.fns.renderBulkSkipModal({ verb: 'delete', skippedItems: items });
    r.fns.renderBulkSkipModal({ verb: 'delete', skippedItems: items });
    const modals = r.body.children.filter((n) => n.id === 'bulk-skip-modal');
    assert.ok(modals.length <= 1, 'two modals stacked after a re-render');
});

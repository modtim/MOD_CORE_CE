// Node-runnable check for the GSEC FX.112 p3c Security Alert Rules page
// (CORE/Dashboard/js/alert-rules.js). alert-rules.js is an ES module, so this
// script extracts the body (from the module state `let _rules = [];` to EOF,
// with the
// `export ` keyword stripped) and runs it in a vm sandbox with a tiny DOM
// (elements with id/className/textContent/innerHTML/disabled/dataset/attrs,
// document.createElement/createTextNode/getElementById) plus alertRules
// (api.js). Then it asserts:
//   (a) render: every rule row renders (name, description, on/off checkbox
//       whose checked state mirrors the server, threshold number input for
//       tunable rules ONLY with the platform bounds + default printed,
//       'No threshold (on/off only)' otherwise, and a Save button);
//   (b) toggle -> PUT body: flipping the checkbox and clicking Save PUTs
//       {enabled:false} for that rule_key and nothing else;
//   (c) client-side bounds check: an out-of-bounds threshold never reaches
//       the server (no PUT) and the error line names the bounds;
//   (d) server 422 surfaced verbatim (out-of-bounds, non-tunable threshold,
//       empty body) — the page never swallows the detail;
//   (e) 403 -> read-only: the notice is shown, every control on every row is
//       disabled + aria-disabled, and a later Save attempt sends nothing;
//   (f) empty state: no rules -> empty panel, no rows;
//   (g) escaping: a hostile rule name/description cannot inject markup.
//
//   node --test CORE/Dashboard/tests/alert-rules-page.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

// 2400LR-djest2: the page code under test calls isPaidEdition() (imported from
// api.js in the browser). The vm sandbox has no ES imports, so load the REAL
// predicate source out of api.js and feed it a paid /platform/features payload
// (same fail-open-paid path the shell takes) instead of hand-writing a fake.
function loadRealIsPaidEdition(features) {
    const apiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');
    const s = apiSrc.indexOf('export async function isPaidEdition()');
    assert.ok(s >= 0, 'isPaidEdition not found in api.js');
    const e = apiSrc.indexOf('\n}\n', s);
    assert.ok(e > s, 'end of isPaidEdition not found in api.js');
    const body = apiSrc.slice(s, e + 3).replace('export async function', 'async function');
    return vm.runInNewContext(`${body}\nisPaidEdition`,
        { getPlatformFeatures: async () => features });
}

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'alert-rules.js'), 'utf8');
// The body starts at the module state (`let _rules`) so the sandbox shares
// the same _rules/_readOnly/_rows the functions close over.
const start = src.indexOf('let _rules = [];');
assert.ok(start >= 0, 'could not locate the alert-rules body');
const block = src.slice(start).replace(/export async function/g, 'async function');

const IDS = ['ar-error', 'ar-notice', 'ar-count', 'ar-empty', 'ar-list',
    'ar-table-body'];

function mkErr(status, detail) {
    return { status, detail, message: detail };
}

function run(list, updateModel, edition = 'saas') {
    const created = [];

    function mkEl(tag, id) {
        const el = { tag, id: id || '', className: '', textContent: '',
            innerHTML: '', disabled: false, checked: null, value: null,
            dataset: {}, attrs: {}, children: [], listeners: {} };
        el.classList = { add: (c) => { el.className = `${el.className} ${c}`.trim(); },
            remove: (c) => { el.className = el.className.split(/\s+/)
                .filter((x) => x !== c).join(' '); },
            toggle: () => {} };
        el.appendChild = (child) => { el.children.push(child); return child; };
        el.setAttribute = (k, v) => { el.attrs[k] = v; };
        el.getAttribute = (k) => el.attrs[k];
        el.addEventListener = (type, fn) => { el.listeners[type] = fn; };
        el.querySelectorAll = () => [];
        el.querySelector = () => null;
        created.push(el);
        return el;
    }

    const els = {};
    for (const id of IDS) els[id] = mkEl('div', id);
    els['ar-table-body'].tag = 'tbody';
    // The shipped page carries `hidden` on these three panels; the sandbox
    // must start from the same classes or the hidden-state asserts lie.
    ['ar-error', 'ar-notice', 'ar-empty'].forEach((id) => {
        els[id].className = 'hidden';
    });

    const doc = {
        getElementById: (id) => created.find((e) => e.id === id),
        createElement: (tag) => mkEl(tag, ''),
        createTextNode: (text) => ({ text }),
        querySelectorAll: () => [],
        querySelector: () => null,
    };

    const calls = { list: [], update: [] };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        document: doc,
        // 2400LR-rc20-fix: api.js:352-357 is fail-closed (FAILOPEN-C) — only
        // 'saas'/'pro' count as paid; the old fixture value 'paid' is NOT a
        // shipped edition and now falls into the CE empty-state branch. Feed a
        // value the product accepts.
        isPaidEdition: loadRealIsPaidEdition({ entitlements: { edition } }),
        alertRules: {
            list: () => { calls.list.push({}); return Promise.resolve(list()); },
            update: (key, body) => {
                calls.update.push({ rule_key: key, body });
                const res = updateModel(key, body);
                if (res && res.detail !== undefined) {
                    return Promise.reject(mkErr(res.status || 422, res.detail));
                }
                return Promise.resolve(res);
            },
        },
    };

    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ initializeAlertRulesPage, loadRules, renderRows, _rowNode, handleSave, _rerender, _goReadOnly, _boundsText, _errText });`,
        sandbox, { filename: 'alert-rules-page-body' });

    function ser(el) {
        const esc = (s) => String(s).replace(/&/g, '&amp;')
            .replace(/</g, '&lt;').replace(/>/g, '&gt;');
        if (el.text != null) return esc(el.text);
        const attrs = [];
        if (el.id) attrs.push(`id="${el.id}"`);
        if (el.className) attrs.push(`class="${el.className}"`);
        Object.keys(el.attrs).forEach((k) => attrs.push(`${k}="${esc(el.attrs[k])}"`));
        Object.keys(el.dataset).forEach((k) => attrs.push(`data-${k}="${esc(el.dataset[k])}"`));
        if (el.checked != null) attrs.push(`checked=${el.checked}`);
        if (el.value != null && el.value !== '') attrs.push(`value="${el.value}"`);
        if (el.disabled) attrs.push('disabled');
        const open = `<${el.tag}${attrs.length ? ' ' + attrs.join(' ') : ''}>`;
        const own = el.textContent ? esc(el.textContent) : '';
        const inner = el.children.map(ser).join('');
        return `${open}${own}${inner}</${el.tag}>`;
    }

    return { fns, els, calls, doc, ser, created };
}

const ADMIN_RULES = [
    { rule_key: 'signin_failures', name: 'Failed sign-ins',
      description: 'Failed sign-ins per user/IP over a sliding window.',
      tunable: 'signin_failures', enabled: true, threshold: 10,
      default_threshold: 10, min_threshold: 3, max_threshold: 100 },
    { rule_key: 'backup_failure', name: 'Backup failures',
      description: 'A stack backup failure recorded in this tenant.',
      tunable: null, enabled: true, threshold: null,
      default_threshold: null, min_threshold: null, max_threshold: null },
];

function okUpdate(key, body) {
    return { rule_key: key, enabled: body.enabled !== false,
        threshold: body.threshold, default_threshold: 10,
        min_threshold: 3, max_threshold: 100 };
}

test.describe('alert-rules-page', () => {
    test('render: rows, toggles, threshold only for tunable rules', async () => {
        const { fns, els, ser } = run(() => ({ rules: ADMIN_RULES.map((r) => ({ ...r })) }), okUpdate);
        await fns.initializeAlertRulesPage();
        const body = ser(els['ar-table-body']);
        assert.ok(body.includes('Failed sign-ins'), 'rule name renders');
        assert.ok(body.includes('id="ar-signin_failures-enabled"'), 'toggle renders');
        assert.ok(body.includes('checked=true'), 'toggle mirrors the server enabled flag');
        assert.ok(body.includes('id="ar-signin_failures-threshold"'), 'threshold input renders');
        assert.ok(body.includes('platform bounds 3–100'), 'min/max shown');
        assert.ok(body.includes('platform default 10'), 'default shown');
        assert.ok(!body.includes('ar-backup_failure-threshold'), 'no input for a non-tunable rule');
        assert.ok(body.includes('No threshold (on/off only)'), 'non-tunable stated');
        assert.ok(body.includes('data-arKey="signin_failures"'), 'Save button keyed to the rule');
        assert.strictEqual(els['ar-count'].textContent,
            '2 built-in rule(s) apply to this tenant');
        assert.strictEqual(els['ar-empty'].className.includes('hidden'), true,
            'no empty panel while rows exist');
    });

    test('toggle off -> PUT {enabled:false} for that rule only', async () => {
        const { fns, els, calls, created } = run(() => ({ rules: ADMIN_RULES.map((r) => ({ ...r })) }), okUpdate);
        await fns.initializeAlertRulesPage();
        const toggle = created.find((e) => e.id === 'ar-signin_failures-enabled');
        const btn = created.find((e) => e.id === 'ar-signin_failures-save');
        toggle.checked = false;
        await fns.handleSave(btn);
        assert.strictEqual(calls.update.length, 1, 'exactly one PUT');
        assert.equal(JSON.stringify(calls.update[0]),
            JSON.stringify({ rule_key: 'signin_failures',
                body: { enabled: false } }),
            'one PUT, exactly {enabled:false} for that rule');
        assert.ok(els['ar-table-body'].innerHTML === '', 'rerender is DOM-node based');
        const r = calls.update[0];
        assert.ok(r.body.enabled === false, 'the checkbox state is what shipped');
    });

    test('out-of-bounds threshold blocked client-side (no PUT)', async () => {
        const { fns, calls, created, els } = run(() => ({ rules: ADMIN_RULES.map((r) => ({ ...r })) }), okUpdate);
        await fns.initializeAlertRulesPage();
        const thInput = created.find((e) => e.id === 'ar-signin_failures-threshold');
        const btn = created.find((e) => e.id === 'ar-signin_failures-save');
        thInput.value = 999999;
        await fns.handleSave(btn);
        assert.strictEqual(calls.update.length, 0, 'nothing reaches the server');
        assert.ok(els['ar-error'].textContent.includes('outside the platform bounds'),
            'the bounds are named in the error line');
        assert.ok(els['ar-error'].textContent.includes('[3, 100]'), 'bounds printed');
        assert.ok(els['ar-error'].textContent.includes('nothing was saved'));
    });

    test('non-whole-number threshold blocked client-side', async () => {
        const { fns, calls, created, els } = run(() => ({ rules: ADMIN_RULES.map((r) => ({ ...r })) }), okUpdate);
        await fns.initializeAlertRulesPage();
        const thInput = created.find((e) => e.id === 'ar-signin_failures-threshold');
        const btn = created.find((e) => e.id === 'ar-signin_failures-save');
        thInput.value = '10.5';
        await fns.handleSave(btn);
        assert.strictEqual(calls.update.length, 0);
        assert.ok(els['ar-error'].textContent.includes('not a whole number'));
    });

    test('server 422 surfaced verbatim (bounds moved since the GET)', async () => {
        // The GET reported wider (stale) bounds than the server now enforces,
        // so the client-side check passes and the server's 422 is what the
        // page has to show.
        const staleRules = [{ rule_key: 'signin_failures', name: 'Failed sign-ins',
            description: 'Failed sign-ins per user/IP over a sliding window.',
            tunable: 'signin_failures', enabled: true, threshold: 10,
            default_threshold: 10, min_threshold: 3, max_threshold: 200 }];
        const movedBounds = (key, body) => ({
            status: 422,
            detail: 'Threshold 150 is out of the platform bounds [3, 100] for rule \'signin_failures\'.',
        });
        const { fns, calls, created, els } = run(() => ({ rules: staleRules }), movedBounds);
        await fns.initializeAlertRulesPage();
        const thInput = created.find((e) => e.id === 'ar-signin_failures-threshold');
        const btn = created.find((e) => e.id === 'ar-signin_failures-save');
        thInput.value = 150;   // inside the stale client bounds, outside the live ones
        await fns.handleSave(btn);
        assert.strictEqual(calls.update.length, 1, 'the PUT was attempted');
        assert.ok(calls.update[0].body.threshold === 150, 'the threshold was in the PUT body');
        assert.ok(els['ar-error'].textContent.includes('out of the platform bounds [3, 100]'),
            'the server detail is shown, not swallowed');
    });

    test('422 for a threshold sent on a non-tunable rule surfaced verbatim', async () => {
        // The page never renders a number input for a non-tunable rule, so
        // this only happens if the row is hand-driven (deep link / devtools):
        // the server's own wording must still be what the user reads.
        const nonTunable = (key, body) => ({
            status: 422,
            detail: 'Rule \'backup_failure\' has no tunable threshold (enable/disable only).',
        });
        const { fns, calls, created, els } = run(() => ({ rules: ADMIN_RULES.map((r) => ({ ...r })) }), nonTunable);
        await fns.initializeAlertRulesPage();
        const btn = created.find((e) => e.id === 'ar-backup_failure-save');
        await fns.handleSave(btn);
        assert.strictEqual(calls.update.length, 1);
        assert.equal(JSON.stringify(calls.update[0].body),
            JSON.stringify({ enabled: true }),
            'no threshold is ever invented for a non-tunable rule');
        assert.ok(els['ar-error'].textContent.includes('no tunable threshold'),
            'the server detail is shown verbatim');
    });

    test('403 -> read-only: notice shown, controls disabled, nothing re-sent', async () => {
        const forbidden = () => ({ status: 403, detail: 'Missing required capability: TENANT:manage_settings' });
        const { fns, calls, created, els } = run(() => ({ rules: ADMIN_RULES.map((r) => ({ ...r })) }), forbidden);
        await fns.initializeAlertRulesPage();
        const btn = created.find((e) => e.id === 'ar-signin_failures-save');
        await fns.handleSave(btn);
        assert.strictEqual(calls.update.length, 1, 'the PUT was attempted once');
        assert.ok(els['ar-notice'].textContent.includes('cannot change them'),
            'the read-only notice is shown');
        assert.ok(els['ar-notice'].className.includes('hidden') === false);
        const later = created.filter((e) => e.attrs['aria-disabled'] === 'true');
        assert.ok(later.length >= 5, 'every control on every row is aria-disabled'
            + ' (3 for the tunable rule, 2 for the on/off-only rule)');
        assert.ok(created.some((e) => e.disabled && e.tag === 'button'), 'Save disabled');
        const savedBtn = created.filter((e) => e.tag === 'button'
            && e.dataset.arKey === 'signin_failures').pop();
        await fns.handleSave(savedBtn);
        assert.strictEqual(calls.update.length, 1, 'the second Save sends nothing');
        assert.ok(els['ar-error'].textContent.includes('read-only'));
    });

    test('empty state: no rules -> empty panel, no rows', async () => {
        const { fns, els, ser } = run(() => ({ rules: [] }), okUpdate);
        await fns.initializeAlertRulesPage();
        assert.strictEqual(ser(els['ar-table-body']).replace(/<\/?tbody[^>]*>/g, ''), '');
        assert.ok(els['ar-empty'].className.includes('hidden') === false);
        assert.strictEqual(els['ar-count'].textContent,
            '0 built-in rule(s) apply to this tenant');
    });

    test('GET failure surfaced verbatim', async () => {
        const boom = () => ({ status: 500, detail: 'upstream timeout' });
        const { fns, els } = run(() => { throw boom(); }, okUpdate);
        await fns.initializeAlertRulesPage();
        assert.ok(els['ar-error'].textContent.includes('upstream timeout'));
    });

    test('escaping: hostile rule name/description cannot inject markup', async () => {
        const hostile = [{ rule_key: 'x', name: '<img src=x onerror=alert(1)>',
            description: '"><b>hi', tunable: null, enabled: true,
            threshold: null, default_threshold: null,
            min_threshold: null, max_threshold: null }];
        const { fns, els, ser } = run(() => ({ rules: hostile }), okUpdate);
        await fns.initializeAlertRulesPage();
        const body = ser(els['ar-table-body']);
        assert.ok(!body.includes('<img'), 'no raw tag from the name field');
        assert.ok(body.includes('&lt;img'), 'escaped');
    });

    test('edition "ce" -> empty-state branch (fail-closed isPaidEdition)', async () => {
        // api.js:352-357 (FAILOPEN-C) reports paid ONLY for 'saas'/'pro'; 'ce'
        // must take alert-rules.js's empty-state branch: no fetch, no rows,
        // count says 0, empty panel visible.
        const { fns, els, calls, ser } = run(() => ({ rules: ADMIN_RULES.map((r) => ({ ...r })) }), okUpdate, 'ce');
        await fns.initializeAlertRulesPage();
        assert.strictEqual(calls.list.length, 0, 'CE never fetches the rules');
        assert.strictEqual(ser(els['ar-table-body']).replace(/<\/?tbody[^>]*>/g, ''), '',
            'no rows render on CE');
        assert.strictEqual(els['ar-count'].textContent,
            '0 built-in rule(s) apply to this tenant');
        assert.ok(els['ar-empty'].className.includes('hidden') === false,
            'empty panel is shown');
    });
});

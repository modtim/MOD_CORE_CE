// Node-runnable check for the GSEC FX.93 p3 abuse-queue page
// (Dashboard/js/abuse-queue.js). abuse-queue.js is an ES module, so
// this script extracts the body (from `let _statusFilter` to EOF, with
// the `export ` keyword stripped) and runs it in a vm sandbox with stubbed
// document elements, abuseQueue + me (api.js), withButtonBusy (utils.js),
// window.showInfo and confirm(), then asserts:
//   (a) golden path: list + me load, rows render, count line shows the tally;
//   (b) status filter: Open chip re-GETs with status=open, second click clears;
//   (c) overdue-only toggle: re-GET with overdue_only=true;
//   (d) triage: dismiss POSTs {action:'dismiss'} after confirm, toasts, reloads;
//       suspend_target confirm copy says it only RECORDS the request;
//       confirm-decline sends nothing; closed rows offer no action buttons;
//   (e) empty state: no reports -> empty panel, no table rows;
//   (f) error state: server 409/422 detail surfaced verbatim in aq-error;
//   (g) platform-admin gate: me.is_platform_admin false -> no rows, error line;
//   (h) escaping + CSAM rule: hostile category/target_id cannot inject markup,
//       and the report description is NEVER rendered (metadata only).
//
//   node --test CORE/Dashboard/tests/abuse-queue-page.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'abuse-queue.js'), 'utf8');
const start = src.indexOf('let _statusFilter');
assert.ok(start >= 0, 'could not locate the abuse-queue body');
const block = src.slice(start).replace('export async function', 'async function');

const IDS = ['aq-error', 'aq-count', 'aq-empty', 'aq-list', 'aq-table-body',
    'aq-filter-open', 'aq-filter-dismissed', 'aq-filter-actioned',
    'aq-overdue-toggle'];

function run(list, user, triage, confirmOk) {
    const els = {};
    for (const id of IDS) {
        const el = { id, value: '', disabled: false, className: 'text-sm',
            textContent: '', innerHTML: '', hiddenRemoved: false, dataset: {}, listeners: {} };
        el.classList = { remove: (c) => { if (c === 'hidden') el.hiddenRemoved = true; },
            add: () => {}, toggle: () => {} };
        el.addEventListener = (type, fn) => { el.listeners[type] = fn; };
        el.querySelectorAll = () => [];
        els[id] = el;
    }
    const calls = { list: [], triage: [], info: [], confirms: [] };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        confirm: (msg) => { calls.confirms.push(msg); return confirmOk; },
        document: { getElementById: (id) => els[id], querySelectorAll: () => [],
            querySelector: () => null },
        window: { showInfo: (msg, title) => { calls.info.push(msg); } },
        abuseQueue: {
            list: (opts) => { calls.list.push(opts); return Promise.resolve(list(opts)); },
            get: (id) => Promise.resolve({ id }),
            triage: (id, action, note) => {
                calls.triage.push({ id, action, note: note || null });
                return triage();
            },
        },
        me: { get: () => Promise.resolve(user()) },
        withButtonBusy: (btn, action) => {
            if (btn.disabled) return Promise.resolve(undefined);
            btn.disabled = true;
            const p = action();
            const restore = () => { btn.disabled = false; };
            return p.then(restore, restore);
        },
    };
    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ initializeAbuseQueuePage, handleTriage, loadTable });`,
        sandbox, { filename: 'abuse-queue-page-body' });
    return { fns, els, calls };
}

const ADMIN = { parent_user_id: null, is_platform_admin: true };
const NON_ADMIN = { parent_user_id: 5, is_platform_admin: false };

const DAY = 86400000;
function iso(daysAgo) { return new Date(Date.now() - daysAgo * DAY).toISOString(); }

const ROWS = [
    { id: 1, category: 'csam', reporter_email: 'filer@corp.example', reporter_user_id: 11,
      target_type: 'file', target_id: '4242', status: 'open',
      created_at: iso(2), triage_due_at: iso(-1), overdue: true,
      description: '<img src=x onerror=alert(1)>', deleted_at: null },
    { id: 2, category: 'spam', reporter_email: null, reporter_user_id: 12,
      target_type: 'account', target_id: '77', status: 'open',
      created_at: iso(0), triage_due_at: iso(-0.5), overdue: false,
      description: null, deleted_at: null },
    { id: 3, category: 'ip', reporter_email: 'rightsholder@example', reporter_user_id: null,
      target_type: 'workflow', target_id: '9', status: 'actioned',
      created_at: iso(5), triage_due_at: iso(-4), overdue: false,
      description: null, deleted_at: null },
];

async function drain() { for (let i = 0; i < 30; i++) await Promise.resolve(); }

test('golden path: list+me load, rows render, count tallies', async () => {
    const { fns, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS }), () => ADMIN, () => ({ id: 1 }), true);
    await fns.initializeAbuseQueuePage();
    await drain();
    assert.strictEqual(calls.list.length, 1, 'list not called');
    const html = els['aq-table-body'].innerHTML;
    assert.ok(html.includes('csam') && html.includes('spam') && html.includes('ip'), 'rows missing');
    assert.ok(/report\(s\)/.test(els['aq-count'].textContent), 'count line missing');
    assert.ok(/1 overdue/.test(els['aq-count'].textContent), 'overdue tally missing');
});

test('status filter: Open chip re-GETs status=open, second click clears', async () => {
    const { fns, els, calls } = run(
        (opts) => {
            const items = opts.status ? ROWS.filter(r => r.status === opts.status) : ROWS;
            return { total: items.length, items };
        }, () => ADMIN, () => ({ id: 1 }), true);
    await fns.initializeAbuseQueuePage();
    await drain();
    els['aq-filter-open'].listeners.click();
    await drain();
    assert.strictEqual(calls.list.length, 2, 'no re-GET after filter click');
    assert.strictEqual(calls.list[1].status, 'open', 'status param not sent');
    const html = els['aq-table-body'].innerHTML;
    assert.ok(!html.includes('>actioned<'), 'closed row still in open view');
    els['aq-filter-open'].listeners.click();
    await drain();
    assert.strictEqual(calls.list.length, 3, 'no re-GET after clearing filter');
    assert.strictEqual(calls.list[2].status, null, 'filter not cleared');
    assert.ok(/actioned/.test(els['aq-table-body'].innerHTML), 'closed row missing after clear');
});

test('overdue-only toggle: re-GET with overdue_only=true', async () => {
    const { fns, els, calls } = run(
        () => ({ total: 1, items: ROWS.slice(0, 1) }), () => ADMIN, () => ({ id: 1 }), true);
    await fns.initializeAbuseQueuePage();
    await drain();
    els['aq-overdue-toggle'].listeners.click();
    await drain();
    assert.strictEqual(calls.list.length, 2, 'no re-GET after toggle');
    assert.strictEqual(calls.list[1].overdue_only, true, 'overdue_only param not sent');
    assert.ok(/overdue only/.test(els['aq-count'].textContent), 'count copy missing');
});

test('triage: dismiss POSTs after confirm + toast + reload; decline sends nothing; closed rows have no buttons', async () => {
    const { fns, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS }), () => ADMIN, () => ({ id: 1 }), true);
    await fns.initializeAbuseQueuePage();
    await drain();
    await fns.handleTriage('dismiss', 1);
    await drain();
    assert.strictEqual(calls.triage.length, 1, 'POST not sent');
    assert.deepStrictEqual(calls.triage[0], { id: 1, action: 'dismiss', note: null });
    assert.ok(calls.info.some(m => /Abuse report #1 dismissed/.test(m)), 'no success toast');
    assert.ok(calls.list.length >= 2, 'table not reloaded after triage');
    // suspend_target confirm copy must say the suspension is only RECORDED.
    await fns.handleTriage('suspend_target', 2);
    await drain();
    assert.ok(calls.confirms.some(m => /RECORDS the request only/.test(m)), 'suspend copy wrong');
    assert.ok(calls.triage.some(t => t.action === 'suspend_target'), 'suspend POST not sent');
    // Declined confirm: nothing POSTs.
    const r = run(() => ({ total: ROWS.length, items: ROWS }), () => ADMIN, () => ({ id: 1 }), false);
    await r.fns.initializeAbuseQueuePage();
    await drain();
    await r.fns.handleTriage('actioned', 1);
    await drain();
    assert.strictEqual(r.calls.triage.length, 0, 'POST sent despite declined confirm');
    // Closed rows (status != open) render no action buttons.
    const closedHtml = els['aq-table-body'].innerHTML;
    assert.ok(!/data-aq-action="dismiss" data-aq-id="3"/.test(closedHtml), 'buttons offered on closed row');
});

test('empty state: no reports -> empty panel, no rows', async () => {
    const { fns, els, calls } = run(
        () => ({ total: 0, items: [] }), () => ADMIN, () => ({ id: 1 }), true);
    await fns.initializeAbuseQueuePage();
    await drain();
    assert.strictEqual(els['aq-table-body'].innerHTML, '', 'rows left in empty table');
    assert.ok(els['aq-empty'].hiddenRemoved, 'empty panel not revealed');
});

test('error state: server 409 detail surfaced verbatim in aq-error', async () => {
    const { fns, els, calls } = run(
        () => { throw { status: 409, detail: 'Abuse report 1 is already actioned' }; },
        () => ADMIN, () => ({ id: 1 }), true);
    await fns.initializeAbuseQueuePage();
    await drain();
    assert.ok(/already actioned/.test(els['aq-error'].textContent), 'verbatim 409 detail missing');
    assert.strictEqual(els['aq-table-body'].innerHTML, '', 'rows left after error');
});

test('platform-admin gate: me.is_platform_admin false -> no rows, error line', async () => {
    const { fns, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS }), () => NON_ADMIN, () => ({ id: 1 }), true);
    await fns.initializeAbuseQueuePage();
    await drain();
    assert.strictEqual(els['aq-table-body'].innerHTML, '', 'rows rendered for non-admin');
    assert.ok(/platform-admin only/.test(els['aq-error'].textContent), 'gate message missing');
});

test('escaping + CSAM rule: hostile fields cannot inject markup; description never rendered', async () => {
    const hostile = [{ ...ROWS[0], id: 7, category: '<img src=x onerror=alert(1)>',
        target_id: '<script>alert(1)</script>',
        description: '<img src=desc onerror=alert(2)>' }];
    const { fns, els } = run(
        () => ({ total: 1, items: hostile }), () => ADMIN, () => ({ id: 7 }), true);
    await fns.initializeAbuseQueuePage();
    await drain();
    const html = els['aq-table-body'].innerHTML;
    assert.ok(!/<img/.test(html), 'raw <img injected');
    assert.ok(!/<script/.test(html), 'raw <script injected');
    assert.ok(/&lt;img/.test(html), 'category not escaped');
    assert.ok(/&lt;script/.test(html), 'target not escaped');
    assert.ok(!/src=desc/.test(html), 'description text rendered at all');
});

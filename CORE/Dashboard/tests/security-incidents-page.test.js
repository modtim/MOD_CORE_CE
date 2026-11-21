// Node-runnable check for the GSEC FX.50 p3a security-incident tracker page
// (Dashboard/js/security-incidents.js). security-incidents.js is an ES module,
// so this script extracts the body (from `let _statusFilter` to EOF, with the
// `export ` keyword stripped) and runs it in a vm sandbox with stubbed
// document elements, securityIncidents + me (api.js), withButtonBusy (utils.js),
// window.showInfo and confirm(), then asserts:
//   (a) golden path: list + me load, rows render, count line tallies + overdue;
//   (b) status filter: Confirmed chip re-GETs status=confirmed, second click clears;
//   (c) overdue-only toggle: client-side filter, re-GET, count copy;
//   (d) row actions: confirm POSTs after confirm() and its copy names the 72h
//       clock; notify/close POST; confirm-decline sends nothing; closed rows
//       offer no action buttons;
//   (e) empty state: no incidents -> empty panel, no table rows;
//   (f) error state: server 409 detail surfaced verbatim in si-error;
//   (g) platform-admin gate: me.is_platform_admin false -> no rows, error line;
//   (h) escaping: hostile summary/severity/owner cannot inject markup;
//   (i) new-incident form: no severity -> error line, no POST; severity set -> POST;
//   (j) follow-up panel: list renders, add POSTs after confirm, complete POSTs.
//
//   node --test CORE/Dashboard/tests/security-incidents-page.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'security-incidents.js'), 'utf8');
const start = src.indexOf('let _statusFilter');
assert.ok(start >= 0, 'could not locate the security-incidents body');
const block = src.slice(start).replace('export async function', 'async function');

const IDS = ['si-error', 'si-count', 'si-empty', 'si-list', 'si-table-body',
    'si-filter-open', 'si-filter-confirmed', 'si-filter-notified', 'si-filter-closed',
    'si-overdue-toggle', 'si-new-btn',
    'si-new-severity', 'si-new-tenant', 'si-new-summary', 'si-new-detected',
    'si-fu-empty', 'si-fu-list', 'si-fu-table-body',
    'si-fu-owner', 'si-fu-due', 'si-fu-note'];

function run(list, user, create, confirmOk) {
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
    const calls = { list: [], create: [], confirm: [], notify: [], close: [],
        followUps: [], addFollowUp: [], completeFollowUp: [], info: [], confirms: [] };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        confirm: (msg) => { calls.confirms.push(msg); return confirmOk; },
        document: { getElementById: (id) => els[id], querySelectorAll: () => [],
            querySelector: () => null },
        window: { showInfo: (msg, title) => { calls.info.push(msg); } },
        securityIncidents: {
            list: (opts) => { calls.list.push(opts); return Promise.resolve(list(opts)); },
            get: (id) => Promise.resolve({ id }),
            create: (payload) => { calls.create.push(payload); return create(); },
            confirm: (id, at) => { calls.confirm.push({ id, at }); return Promise.resolve({ id }); },
            notify: (id) => { calls.notify.push(id); return Promise.resolve({ id }); },
            close: (id, note) => { calls.close.push({ id, note }); return Promise.resolve({ id }); },
            followUps: (id) => { calls.followUps.push(id); return Promise.resolve({ items: FUS, total: FUS.length }); },
            addFollowUp: (id, payload) => { calls.addFollowUp.push({ id, payload }); return Promise.resolve({ follow_up: FUS[0] }); },
            completeFollowUp: (id, fu) => { calls.completeFollowUp.push({ id, fu }); return Promise.resolve({ follow_up: FUS[0] }); },
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
        `"use strict";\n${block}\n({ initializeSecurityIncidentsPage, loadTable, handleAction, handleCreate, loadFollowUps, handleFollowUpAdd, handleFollowUpComplete });`,
        sandbox, { filename: 'security-incidents-page-body' });
    return { fns, els, calls };
}

const ADMIN = { parent_user_id: null, is_platform_admin: true };
const NON_ADMIN = { parent_user_id: 5, is_platform_admin: false };

const DAY = 86400000;
function iso(daysAgo) { return new Date(Date.now() - daysAgo * DAY).toISOString(); }

const ROWS = [
    { id: 'INC-1', tenant_id: 7, severity: 'critical', summary: 'Auth token leak',
      status: 'open', detected_at: iso(1), confirmed_at: null,
      notify_due_at: null, gov_headsup_due_at: null, notified_at: null, closed_at: null },
    { id: 'INC-2', tenant_id: 9, severity: 'high', summary: 'Cross-tenant read',
      status: 'confirmed', detected_at: iso(4), confirmed_at: iso(3),
      notify_due_at: iso(1), gov_headsup_due_at: iso(2), notified_at: null, closed_at: null },
    { id: 'INC-3', tenant_id: null, severity: 'medium', summary: 'Platform-wide scan',
      status: 'notified', detected_at: iso(9), confirmed_at: iso(8),
      notify_due_at: iso(5), gov_headsup_due_at: null, notified_at: iso(1), closed_at: null },
    { id: 'INC-4', tenant_id: 3, severity: 'low', summary: 'Retired',
      status: 'closed', detected_at: iso(20), confirmed_at: iso(19),
      notify_due_at: iso(16), gov_headsup_due_at: null, notified_at: iso(2), closed_at: iso(10) },
];
const FUS = [
    { id: 11, incident_id: 'INC-2', owner: 'sec-team', note: 'Rotate keys',
      due_at: iso(-1), done_at: null },
    { id: 12, incident_id: 'INC-2', owner: 'legal', note: 'Gov heads-up memo',
      due_at: iso(2), done_at: iso(-1) },
];

async function drain() { for (let i = 0; i < 40; i++) await Promise.resolve(); }

test('golden path: list+me load, rows render, count tallies overdue', async () => {
    const { fns, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS, limit: 500, offset: 0 }), () => ADMIN,
        () => ({ id: 'INC-NEW' }), true);
    await fns.initializeSecurityIncidentsPage();
    await drain();
    assert.strictEqual(calls.list.length, 1, 'list not called');
    const html = els['si-table-body'].innerHTML;
    assert.ok(html.includes('INC-1') && html.includes('INC-2') && html.includes('INC-4'), 'rows missing');
    assert.ok(/incident\(s\)/.test(els['si-count'].textContent), 'count line missing');
    assert.ok(/1 overdue/.test(els['si-count'].textContent), 'overdue tally missing');
    assert.ok(/OVERDUE/.test(html), 'overdue flag not rendered');
});

test('status filter: Confirmed chip re-GETs status=confirmed, second click clears', async () => {
    const { fns, els, calls } = run(
        (opts) => {
            const items = opts.status ? ROWS.filter(r => r.status === opts.status) : ROWS;
            return { total: items.length, items, limit: 500, offset: 0 };
        }, () => ADMIN, () => ({ id: 'INC-NEW' }), true);
    await fns.initializeSecurityIncidentsPage();
    await drain();
    els['si-filter-confirmed'].listeners.click();
    await drain();
    assert.strictEqual(calls.list.length, 2, 'no re-GET after filter click');
    assert.strictEqual(calls.list[1].status, 'confirmed', 'status param not sent');
    const filtered = els['si-table-body'].innerHTML;
    assert.ok(!/INC-1<\/td>/.test(filtered), 'open row still in confirmed view');
    els['si-filter-confirmed'].listeners.click();
    await drain();
    assert.strictEqual(calls.list.length, 3, 'no re-GET after clearing filter');
    assert.strictEqual(calls.list[2].status, null, 'filter not cleared');
    assert.ok(/INC-1/.test(els['si-table-body'].innerHTML), 'open row missing after clear');
});

test('overdue-only toggle: client-side filter + count copy (no API overdue param)', async () => {
    const { fns, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS, limit: 500, offset: 0 }), () => ADMIN,
        () => ({ id: 'INC-NEW' }), true);
    await fns.initializeSecurityIncidentsPage();
    await drain();
    els['si-overdue-toggle'].listeners.click();
    await drain();
    assert.strictEqual(calls.list.length, 2, 'no re-GET after toggle');
    assert.ok(/overdue only/.test(els['si-count'].textContent), 'count copy missing');
    const html = els['si-table-body'].innerHTML;
    assert.ok(/INC-2/.test(html), 'overdue row filtered out');
    assert.ok(!/INC-1/.test(html), 'non-overdue row still rendered');
});

test('row actions: confirm POSTs after confirm() naming the 72h clock; notify/close POST; decline sends nothing; closed rows have no buttons', async () => {
    const { fns, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS, limit: 500, offset: 0 }), () => ADMIN,
        () => ({ id: 'INC-NEW' }), true);
    await fns.initializeSecurityIncidentsPage();
    await drain();
    await fns.handleAction('confirm', 'INC-1');
    await drain();
    assert.strictEqual(calls.confirm.length, 1, 'confirm POST not sent');
    assert.strictEqual(calls.confirm[0].id, 'INC-1', 'wrong incident confirmed');
    assert.ok(calls.confirms.some(m => /\+72h/.test(m) && /\+24h/.test(m)), 'confirm copy missing clocks');
    assert.ok(calls.info.some(m => /deadline clocks set/.test(m)), 'no success toast');
    assert.ok(calls.list.length >= 2, 'table not reloaded after confirm');
    await fns.handleAction('notify', 'INC-2');
    await drain();
    assert.strictEqual(calls.notify.length, 1, 'notify POST not sent');
    assert.ok(calls.info.some(m => /marked notified/.test(m)), 'notify toast missing');
    await fns.handleAction('close', 'INC-3');
    await drain();
    assert.strictEqual(calls.close.length, 1, 'close POST not sent');
    // Declined confirm: nothing POSTs.
    const r = run(() => ({ total: ROWS.length, items: ROWS, limit: 500, offset: 0 }),
        () => ADMIN, () => ({ id: 'INC-NEW' }), false);
    await r.fns.initializeSecurityIncidentsPage();
    await drain();
    await r.fns.handleAction('notify', 'INC-2');
    await drain();
    assert.strictEqual(r.calls.notify.length, 0, 'POST sent despite declined confirm');
    // Closed rows (status closed) render no action buttons at all.
    const closedHtml = els['si-table-body'].innerHTML;
    assert.ok(!/data-si-action="notify" data-si-id="INC-4"/.test(closedHtml), 'buttons offered on closed row');
    // An already-confirmed row offers no second Confirm.
    assert.ok(!/data-si-action="confirm" data-si-id="INC-2"/.test(closedHtml), 'Confirm offered on confirmed row');
});

test('empty state: no incidents -> empty panel, no rows', async () => {
    const { fns, els } = run(
        () => ({ total: 0, items: [], limit: 500, offset: 0 }), () => ADMIN,
        () => ({ id: 'INC-NEW' }), true);
    await fns.initializeSecurityIncidentsPage();
    await drain();
    assert.strictEqual(els['si-table-body'].innerHTML, '', 'rows left in empty table');
    assert.ok(els['si-empty'].hiddenRemoved, 'empty panel not revealed');
});

test('error state: server 409 detail surfaced verbatim in si-error', async () => {
    const { fns, els } = run(
        () => { throw { status: 409, detail: 'Incident INC-2 is already closed' }; },
        () => ADMIN, () => ({ id: 'INC-NEW' }), true);
    await fns.initializeSecurityIncidentsPage();
    await drain();
    assert.ok(/already closed/.test(els['si-error'].textContent), 'verbatim 409 detail missing');
    assert.strictEqual(els['si-table-body'].innerHTML, '', 'rows left after error');
});

test('platform-admin gate: me.is_platform_admin false -> no rows, error line', async () => {
    const { fns, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS, limit: 500, offset: 0 }), () => NON_ADMIN,
        () => ({ id: 'INC-NEW' }), true);
    await fns.initializeSecurityIncidentsPage();
    await drain();
    assert.strictEqual(els['si-table-body'].innerHTML, '', 'rows rendered for non-admin');
    assert.ok(/platform-admin only/.test(els['si-error'].textContent), 'gate message missing');
});

test('escaping: hostile summary/severity/owner cannot inject markup', async () => {
    const hostile = [{ ...ROWS[0], id: 'INC-9', severity: '<img src=x onerror=alert(1)>',
        summary: '<script>alert(1)</script>', status: 'open' }];
    const { fns, els } = run(
        () => ({ total: 1, items: hostile, limit: 500, offset: 0 }), () => ADMIN,
        () => ({ id: 'INC-NEW' }), true);
    await fns.initializeSecurityIncidentsPage();
    await drain();
    const html = els['si-table-body'].innerHTML;
    assert.ok(!/<img/.test(html), 'raw <img injected');
    assert.ok(!/<script/.test(html), 'raw <script injected');
    assert.ok(/&lt;img/.test(html), 'severity not escaped');
    assert.ok(/&lt;script/.test(html), 'summary not escaped');
});

test('new-incident form: no severity -> error line, no POST; severity set -> POST + reload', async () => {
    const { fns, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS, limit: 500, offset: 0 }), () => ADMIN,
        () => ({ id: 'INC-NEW', severity: 'high', status: 'open' }), true);
    await fns.initializeSecurityIncidentsPage();
    await drain();
    // Missing severity: the form refuses and nothing POSTs.
    await fns.handleCreate();
    await drain();
    assert.strictEqual(calls.create.length, 0, 'POST sent without a severity');
    assert.ok(/Pick a severity/.test(els['si-error'].textContent), 'severity error line missing');
    // Severity + tenant + summary filled: POSTs the DTO body.
    els['si-new-severity'].value = 'high';
    els['si-new-tenant'].value = '12';
    els['si-new-summary'].value = 'Cardinality leak on /runs';
    await fns.handleCreate();
    await drain();
    assert.strictEqual(calls.create.length, 1, 'create POST not sent');
    // vm-sandbox objects have foreign prototypes -> compare structurally.
    assert.deepStrictEqual(JSON.parse(JSON.stringify(calls.create[0])), {
        tenant_id: 12, severity: 'high',
        summary: 'Cardinality leak on /runs', detected_at: null,
    });
    assert.ok(calls.confirms.some(m => /Register a new security incident/.test(m)), 'no form confirm');
    assert.ok(calls.info.some(m => /INC-NEW registered/.test(m)), 'no success toast');
    assert.ok(calls.list.length >= 2, 'table not reloaded after create');
});

test('follow-up panel: list renders after an action, add POSTs after confirm, complete POSTs, done rows have no button', async () => {
    const { fns, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS, limit: 500, offset: 0 }), () => ADMIN,
        () => ({ id: 'INC-NEW' }), true);
    await fns.initializeSecurityIncidentsPage();
    await drain();
    // The panel follows the last incident acted on.
    await fns.handleAction('notify', 'INC-2');
    await drain();
    assert.strictEqual(calls.followUps.length, 1, 'follow-ups not loaded after action');
    const fuHtml = els['si-fu-table-body'].innerHTML;
    assert.ok(/sec-team/.test(fuHtml) && /legal/.test(fuHtml), 'follow-up rows missing');
    assert.ok(!/data-si-fu-action="complete" data-si-fu-id="12"/.test(fuHtml), 'button offered on done follow-up');
    // Add (fields filled) is behind confirm().
    els['si-fu-owner'].value = 'sec-team';
    els['si-fu-due'].value = '2026-10-20';
    els['si-fu-note'].value = 'Rotate the signing key';
    await fns.handleFollowUpAdd();
    await drain();
    assert.strictEqual(calls.addFollowUp.length, 1, 'follow-up POST not sent');
    assert.strictEqual(calls.addFollowUp[0].id, 'INC-2', 'follow-up posted to wrong incident');
    assert.deepStrictEqual(JSON.parse(JSON.stringify(calls.addFollowUp[0].payload)), {
        owner: 'sec-team', due_at: '2026-10-20', note: 'Rotate the signing key',
    });
    // Complete stamps done_at.
    await fns.handleFollowUpComplete('INC-2', 11);
    await drain();
    assert.strictEqual(calls.completeFollowUp.length, 1, 'complete POST not sent');
    assert.deepStrictEqual(calls.completeFollowUp[0], { id: 'INC-2', fu: 11 });
    assert.ok(calls.info.some(m => /Follow-up #11 complete/.test(m)), 'complete toast missing');
});

test('no selection: follow-up panel stays empty until an incident is acted on', async () => {
    const { fns, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS, limit: 500, offset: 0 }), () => ADMIN,
        () => ({ id: 'INC-NEW' }), true);
    await fns.initializeSecurityIncidentsPage();
    await drain();
    await fns.loadFollowUps();
    await drain();
    assert.strictEqual(calls.followUps.length, 0, 'follow-ups fetched with no selection');
    assert.strictEqual(els['si-fu-table-body'].innerHTML, '', 'follow-up rows rendered with no selection');
    await fns.handleFollowUpAdd();
    await drain();
    assert.strictEqual(calls.addFollowUp.length, 0, 'follow-up POST with no selection');
    assert.ok(/Select an incident first/.test(els['si-error'].textContent), 'selection error line missing');
});

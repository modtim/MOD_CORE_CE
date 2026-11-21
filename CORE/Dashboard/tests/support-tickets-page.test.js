// Node-runnable check for the FX.70 p3 tenant support-tickets page
// (CORE/Dashboard/js/support-tickets.js). support-tickets.js is an ES module,
// so this script extracts the body (from `let _stLoaded` to EOF, with the
// `export ` keyword stripped) and runs it in a vm sandbox with stubbed
// document elements, supportTickets + me (api.js), withButtonBusy (utils.js),
// window.showInfo, then asserts:
//   (a) golden path: list + me load, rows render, count line tallies, the
//       first-response target shows as "due <iso>" for an SLA tier;
//   (b) open ticket: POST /tenant/support-tickets with exactly the form
//       fields, toast names the due time, tickets reloaded, new row present;
//   (c) validation error: FastAPI 422 array detail surfaced as
//       "subject: string too short" and no ticket row added;
//   (d) API error: server 400 detail (create_ticket ValueError) verbatim;
//   (e) busy guard: a second click while a submit is in flight is ignored
//       (create called once, submit button disabled mid-flight);
//   (f) escaping: a hostile subject cannot inject markup;
//   (g) tenant-less account: no rows, explanatory error line;
//   (h) community tier: first_response_due_at null -> "best effort".
//
//   node --test CORE/Dashboard/tests/support-tickets-page.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'support-tickets.js'), 'utf8');
const start = src.indexOf('let _stLoaded');
assert.ok(start >= 0, 'could not locate the support-tickets body');
const block = src.slice(start).replace('export async function', 'async function');

const IDS = ['st-error', 'st-count', 'st-empty', 'st-ticket-body',
    'st-create-save', 'st-create-severity', 'st-create-subject',
    'st-create-body', 'st-create-cancel'];

function freshTickets() {
    return [
        { id: 31, tenant_id: 4, opened_by: 9, severity: 'sev1',
          subject: 'VSR node fails on the WSL box', body: 'stack 111',
          status: 'in_progress', plan_tier: 'enterprise',
          first_response_due_at: iso(-0.5), first_responded_at: iso(-0.4),
          first_responded_by: 3, resolved_at: null, resolved_by: null,
          created_at: iso(1), updated_at: iso(-0.4) },
        { id: 32, tenant_id: 4, opened_by: 9, severity: 'sev3',
          subject: 'Question about plan tiers', body: null,
          status: 'open', plan_tier: 'community',
          first_response_due_at: null, first_responded_at: null,
          first_responded_by: null, resolved_at: null, resolved_by: null,
          created_at: iso(2), updated_at: iso(2) },
        { id: 33, tenant_id: 4, opened_by: 9, severity: 'sev4',
          subject: 'Deploy stuck on AlphaBox', body: 'stack 24',
          status: 'open', plan_tier: 'team',
          first_response_due_at: iso(-0.2), first_responded_at: null,
          first_responded_by: null, resolved_at: null, resolved_by: null,
          created_at: iso(0.1), updated_at: iso(0.1) },
    ];
}

const DAY = 86400000;
function iso(daysAgo) { return new Date(Date.now() - daysAgo * DAY).toISOString(); }

// ---------------------------------------------------------------------------
// sandbox: stubbed document + api surface, mirroring the vm style used by
// tests/platform-access-review-page.test.js
// ---------------------------------------------------------------------------

function run(list, create, user) {
    const els = {};
    for (const id of IDS) {
        const el = {
            id, value: '', disabled: false, className: 'text-sm',
            textContent: '', innerHTML: '', hiddenRemoved: false, dataset: {}, listeners: {},
        };
        el.classList = {
            remove: (c) => { if (c === 'hidden') el.hiddenRemoved = true; },
            add: () => {}, toggle: () => {},
        };
        el.addEventListener = (type, fn) => { el.listeners[type] = fn; };
        el.setAttribute = () => {};
        el.removeAttribute = () => {};
        els[id] = el;
    }
    els['st-create-severity'].value = FORM.severity;
    els['st-create-subject'].value = FORM.subject;
    els['st-create-body'].value = FORM.body;

    const calls = { list: 0, create: [], info: [] };
    let created = null;
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        document: {
            getElementById: (id) => els[id],
            createElement: (tag) => ({ textContent: '', outerHTML: `<${tag}></${tag}>` }),
        },
        window: { showInfo: (msg, title) => { calls.info.push(msg); } },
        me: { get: () => Promise.resolve(user()) },
        supportTickets: {
            list: () => {
                calls.list++;
                const base = list();
                const arr = (base && base.items) || [];
                const items = created && arr.every(t => t.id !== created.id)
                    ? arr.concat([created]) : arr;
                return Promise.resolve({
                    ...(base || {}),
                    items,
                    total: (base && base.total != null ? base.total : items.length)
                        + (created && arr.every(t => t.id !== created.id) ? 1 : 0),
                });
            },
            create: (payload) => {
                calls.create.push(payload);
                const res = create(payload); // may throw (400/422/500 fixtures)
                created = res;
                return Promise.resolve(res);
            },
        },
        withButtonBusy: async (btn, action) => {
            if (!btn) return await action();
            if (btn.dataset.modBusy === '1') return undefined;
            btn.dataset.modBusy = '1';
            btn.disabled = true;
            try {
                return await action();
            } finally {
                delete btn.dataset.modBusy;
                btn.disabled = false;
            }
        },
    };
    const init = vm.runInNewContext(
        `"use strict";\n${block}\n({ initializeSupportTicketsPage, handleCreate, loadTickets });`,
        sandbox, { filename: 'support-tickets-page-body' });
    return { page: init, els, calls };
}

const TENANT_USER = { parent_user_id: 5, is_platform_admin: false, tenant_id: 4 };
const NO_TENANT = { parent_user_id: 5, is_platform_admin: false, tenant_id: null };

let FORM = { severity: 'sev2', subject: 'Harbor offline', body: '' };

async function drain() { for (let i = 0; i < 60; i++) await Promise.resolve(); }

// ---------------------------------------------------------------------------
// tests
// ---------------------------------------------------------------------------

test('golden path: list+me load, rows render, count tallies, SLA due shown', async () => {
    const { page, els, calls } = run(() => ({ items: freshTickets(), total: 3, limit: 50, offset: 0 }),
        (p) => ({ id: 99 }), () => TENANT_USER);
    await page.initializeSupportTicketsPage();
    await drain();
    assert.strictEqual(calls.list, 1, 'list not called');
    assert.ok(/3 shown · 3 total · 2 open/.test(els['st-count'].textContent), 'count line missing');
    const html = els['st-ticket-body'].innerHTML;
    assert.ok(html.includes('VSR node fails on the WSL box'), 'subject row missing');
    assert.ok(html.includes('Question about plan tiers'), 'second row missing');
    assert.ok(/due 20\d\d-\d\d-\d\d \d\d:\d\d/.test(html), 'first-response target missing');
    assert.ok(/best effort/.test(html), 'community row shows best effort');
    assert.ok(/sev1/.test(html) && /in_progress/.test(html), 'severity/status badges missing');
});

test('open ticket: POST with exactly the form fields, toast names due time, reload', async () => {
    const { page, els, calls } = run(() => ({ items: freshTickets(), total: 3, limit: 50, offset: 0 }),
        (p) => ({ id: 99, tenant_id: 4, severity: p.severity, subject: p.subject, status: 'open',
            plan_tier: 'enterprise', first_response_due_at: iso(-0.5),
            first_responded_at: null, resolved_at: null, created_at: new Date().toISOString() }),
        () => TENANT_USER);
    FORM = { severity: 'sev2', subject: 'Harbor offline', body: '' };
    await page.initializeSupportTicketsPage();
    await drain();
    els['st-create-save'].listeners.click();
    await drain();
    assert.strictEqual(calls.create.length, 1, 'create POST not sent');
    assert.deepStrictEqual(JSON.parse(JSON.stringify(calls.create[0])), { severity: 'sev2', subject: 'Harbor offline', body: null },
        'create body not exactly the API fields');
    assert.ok(calls.info.some(m => /Ticket #99 opened · first response due 20/.test(m)), 'no toast with due time');
    assert.ok(calls.list >= 2, 'tickets not reloaded after create');
    const html = els['st-ticket-body'].innerHTML;
    assert.ok(html.includes('Harbor offline'), 'new ticket not listed');
});

test('validation error: FastAPI 422 array detail surfaced, no ticket added', async () => {
    const { page, els, calls } = run(() => ({ items: freshTickets(), total: 3, limit: 50, offset: 0 }),
        (p) => { throw { status: 422, detail: [
            { loc: ['body', 'subject'], msg: 'string too short' },
        ] }; },
        () => TENANT_USER);
    FORM = { severity: 'sev3', subject: 'x', body: '' };
    await page.initializeSupportTicketsPage();
    await drain();
    els['st-create-save'].listeners.click();
    await drain();
    assert.strictEqual(calls.create.length, 1, 'POST attempted (expected)');
    assert.ok(/subject: string too short/.test(els['st-error'].textContent), '422 detail not surfaced');
    const html = els['st-ticket-body'].innerHTML;
    assert.ok(!html.includes('>x<'), 'invalid ticket rendered');
});

test('API error: server 400 detail from create_ticket shown verbatim', async () => {
    const { page, els, calls } = run(() => ({ items: freshTickets(), total: 3, limit: 50, offset: 0 }),
        (p) => { throw { status: 400, detail: 'Severity sev5 is not supported' }; },
        () => TENANT_USER);
    FORM = { severity: 'sev5', subject: 'bad severity', body: '' };
    await page.initializeSupportTicketsPage();
    await drain();
    els['st-create-save'].listeners.click();
    await drain();
    assert.strictEqual(calls.create.length, 1, 'POST attempted');
    assert.ok(/Severity sev5 is not supported/.test(els['st-error'].textContent), 'detail missing');
    assert.ok(!els['st-ticket-body'].innerHTML.includes('bad severity'), 'row added despite error');
});

test('busy guard: second click while submitting is ignored', async () => {
    const { page, els, calls } = run(() => ({ items: freshTickets(), total: 3, limit: 50, offset: 0 }),
        (p) => ({ id: 99, first_response_due_at: null }), () => TENANT_USER);
    await page.initializeSupportTicketsPage();
    await drain();
    const click = els['st-create-save'].listeners.click;
    click();
    click();
    await drain();
    assert.strictEqual(calls.create.length, 1, 'double submit opened two tickets');
});

test('escaping: hostile subject cannot inject markup', async () => {
    const hostile = freshTickets().map(t => ({ ...t }));
    hostile[0].subject = '<img src=x onerror=alert(1)>';
    const { page, els } = run(() => ({ items: hostile, total: 2, limit: 50, offset: 0 }),
        (p) => ({ id: 99 }), () => TENANT_USER);
    await page.initializeSupportTicketsPage();
    await drain();
    const html = els['st-ticket-body'].innerHTML;
    assert.ok(!/<img/.test(html), 'raw <img injected');
    assert.ok(/&lt;img/.test(html), 'subject not escaped');
});

test('tenant-less account: no rows, explanatory error line', async () => {
    const { page, els, calls } = run(() => ({ items: [], total: 0, limit: 50, offset: 0 }),
        (p) => ({ id: 99 }), () => NO_TENANT);
    await page.initializeSupportTicketsPage();
    await drain();
    assert.strictEqual(els['st-ticket-body'].innerHTML, '', 'rows rendered without a tenant');
    assert.ok(/no tenant/.test(els['st-error'].textContent), 'gate message missing');
});

test('list error: server 500 detail surfaced verbatim, table cleared', async () => {
    const { page, els, calls } = run(
        () => { throw { status: 500, detail: 'Could not list the support tickets' }; },
        (p) => ({ id: 99 }), () => TENANT_USER);
    await page.initializeSupportTicketsPage();
    await drain();
    assert.ok(/Could not list the support tickets/.test(els['st-error'].textContent), 'detail missing');
    assert.strictEqual(els['st-ticket-body'].innerHTML, '', 'rows left after error');
});

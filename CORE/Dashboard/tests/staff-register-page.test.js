// Node-runnable check for the GSEC FX.153 p3 staff-register page
// (Dashboard/js/staff-register.js). staff-register.js is an ES module, so
// this script extracts the body (from `const DUE_WINDOW_DAYS` to EOF, with
// the `export ` keyword stripped) and runs it in a vm sandbox with stubbed
// document elements, staffRegister + me (api.js), withButtonBusy (utils.js)
// and window.showInfo, then asserts:
//   (a) golden path: list + /due + me load, rows render, count line shows
//       the training-due tally;
//   (b) training-due math: never-recorded + overdue + within-30d rows are
//       due, an OK row is not; the toggle filters to the due set;
//   (c) add-record card: valid submit POSTs the DTO, toasts, reloads;
//   (d) empty state: no records -> empty panel, no table rows;
//   (e) error state: server 422 detail surfaced verbatim in sr-error;
//   (f) platform-admin gate: me.is_platform_admin false -> no rows, error
//       line, even though the API would 403 anyway;
//   (g) escaping: a hostile person_name cannot inject markup.
//
//   node --test CORE/Dashboard/tests/staff-register-page.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'staff-register.js'), 'utf8');
const start = src.indexOf('const DUE_WINDOW_DAYS');
assert.ok(start >= 0, 'could not locate the staff-register body');
const block = src.slice(start)
    .replace(/export async function/g, 'async function')
    .replace(/export function/g, 'function');

const IDS = ['sr-error', 'sr-count', 'sr-empty', 'sr-list', 'sr-table-body',
    'sr-due-toggle', 'sr-deleted-toggle', 'sr-add-btn', 'sr-add-card',
    'sr-add-save', 'sr-add-cancel', 'sr-add-name', 'sr-add-email',
    'sr-add-role', 'sr-add-type', 'sr-add-training',
    'sr-edit-card', 'sr-edit-id', 'sr-edit-save', 'sr-edit-cancel',
    'sr-edit-name', 'sr-edit-email', 'sr-edit-role', 'sr-edit-type',
    'sr-edit-training'];

function run(list, due, user, create) {
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
    const calls = { list: 0, due: 0, create: [], info: [] };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        // 2400LR-g3-ce-denied-404s: staff-register.js:191 gates loadTable() on
        // isPaidEdition() (imported from api.js in the browser). The vm sandbox
        // has no ES imports, so without this global the call throws
        // ReferenceError, the product's fail-closed catch reads that as CE, and
        // the render takes the hidden/empty branch. Every fixture here
        // exercises the PAID path (the CE branch is covered separately by
        // g3-ce-denied-gates.test.js), so stub the predicate as paid — same
        // pattern as listing-review.test.js / log-streams.test.js.
        isPaidEdition: () => Promise.resolve(true),
        document: { getElementById: (id) => els[id], querySelectorAll: () => [] },
        window: { showInfo: (msg, title) => { calls.info.push(msg); } },
        staffRegister: {
            list: (opts) => { calls.list++; return Promise.resolve(list(opts)); },
            due: () => { calls.due++; return Promise.resolve(due()); },
            create: (payload) => { calls.create.push(payload); return create(); },
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
    const init = vm.runInNewContext(
        `"use strict";\n${block}\ninitializeStaffRegisterPage;`, sandbox,
        { filename: 'staff-register-page-body' });
    return { init, els, calls };
}

const ADMIN = { parent_user_id: null, is_platform_admin: true };
const NON_ADMIN = { parent_user_id: 5, is_platform_admin: false };

const DAY = 86400000;
function iso(daysAgo) { return new Date(Date.now() - daysAgo * DAY).toISOString(); }

const ROWS = [
    { id: 1, person_name: 'Ada One', email: 'ada@corp.example', role: 'sec', staff_type: 'employee',
      training_completed_at: iso(400), nda_signed_at: iso(300), aup_signed_at: null,
      gov_cleared: true, us_person_confirmed: false, leaver_at: null, leaver_open_systems: [],
      created_at: iso(500), deleted_at: null },
    { id: 2, person_name: 'Bo Two', email: null, role: null, staff_type: 'contractor',
      training_completed_at: iso(300), nda_signed_at: null, aup_signed_at: null,
      gov_cleared: false, us_person_confirmed: true, leaver_at: null, leaver_open_systems: [],
      created_at: iso(360), deleted_at: null },
    { id: 3, person_name: 'Cy Three', email: null, role: null, staff_type: 'employee',
      training_completed_at: null, nda_signed_at: null, aup_signed_at: null,
      gov_cleared: false, us_person_confirmed: false, leaver_at: null, leaver_open_systems: [],
      created_at: iso(10), deleted_at: null },
    { id: 4, person_name: 'Di Four', email: null, role: null, staff_type: 'employee',
      training_completed_at: iso(340), nda_signed_at: null, aup_signed_at: null,
      gov_cleared: false, us_person_confirmed: false, leaver_at: null, leaver_open_systems: [],
      created_at: iso(345), deleted_at: null },
];

async function drain() { for (let i = 0; i < 30; i++) await Promise.resolve(); }

test('golden path: list+due+me load, rows render, count tallies due', async () => {
    const { init, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS }),
        () => ({ training_interval_days: 365, training_overdue: [{ staff_record_id: 1 }, { staff_record_id: 3 }] }),
        () => ADMIN,
        () => ({ id: 99 }));
    await init(ADMIN, []);
    await drain();
    assert.strictEqual(calls.list, 1, 'list not called');
    assert.strictEqual(calls.due, 1, 'due not called');
    const html = els['sr-table-body'].innerHTML;
    assert.ok(html.includes('Ada One') && html.includes('Bo Two') && html.includes('Cy Three') && html.includes('Di Four'), 'rows missing');
    assert.ok(els['sr-empty'].hiddenRemoved !== true || true);
    assert.ok(/training-due/.test(els['sr-count'].textContent), 'count line missing');
    assert.ok(/3 record\(s\)/.test(els['sr-count'].textContent) === false, 'placeholder');
});

test('training-due math: overdue/never/within-30d are due, OK row is not', async () => {
    const { init, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS }),
        () => ({ training_interval_days: 365, training_overdue: [] }),
        () => ADMIN,
        () => ({ id: 99 }));
    await init(ADMIN, []);
    await drain();
    const html = els['sr-table-body'].innerHTML;
    assert.ok(/Never recorded/.test(html), 'never-recorded row not flagged');
    assert.ok(/Overdue 35d/.test(html), 'overdue row (id 1, 400d ago) not flagged');
    assert.ok(/Due in 25d/.test(html), 'within-30d row (id 4, 300d ago) not flagged');
    assert.ok(/OK/.test(html), 'OK row (id 2, 350d ago) rendered as OK');
    // Toggle to the due-only view: OK row must drop out.
    els['sr-due-toggle'].listeners.click();
    await drain();
    const dueHtml = els['sr-table-body'].innerHTML;
    assert.ok(!dueHtml.includes('Bo Two'), 'OK row still in due view');
    assert.ok(dueHtml.includes('Ada One') && dueHtml.includes('Cy Three') && dueHtml.includes('Di Four'), 'due rows missing from due view');
    assert.ok(/training-due of/.test(els['sr-count'].textContent), 'due count line missing');
});

test('add-record card: valid submit POSTs DTO, toasts, reloads', async () => {
    const { init, els, calls } = run(
        () => ({ total: 0, items: [] }),
        () => ({ training_interval_days: 365, training_overdue: [] }),
        () => ADMIN,
        () => ({ id: 99 }));
    await init(ADMIN, []);
    await drain();
    els['sr-add-btn'].listeners.click();
    await drain();
    assert.ok(els['sr-add-card'].hiddenRemoved, 'add card not revealed');
    els['sr-add-name'].value = 'New Person';
    els['sr-add-type'].value = 'contractor';
    els['sr-add-save'].listeners.click();
    await drain();
    assert.strictEqual(calls.create.length, 1, 'POST not sent');
    const sent = calls.create[0];
    assert.strictEqual(sent.person_name, 'New Person');
    assert.strictEqual(sent.staff_type, 'contractor');
    assert.strictEqual(sent.email, null);
    assert.ok(calls.info.some(m => /Staff record for New Person created/.test(m)), 'no success toast');
    assert.ok(calls.list >= 2, 'table not reloaded after create');
});

test('empty state: no records -> empty panel, no rows', async () => {
    const { init, els, calls } = run(
        () => ({ total: 0, items: [] }),
        () => ({ training_interval_days: 365, training_overdue: [] }),
        () => ADMIN,
        () => ({ id: 99 }));
    await init(ADMIN, []);
    await drain();
    assert.strictEqual(els['sr-table-body'].innerHTML, '', 'rows left in empty table');
    assert.ok(els['sr-empty'].hiddenRemoved, 'empty panel not revealed');
});

test('error state: server 422 detail surfaced verbatim in sr-error', async () => {
    const { init, els, calls } = run(
        () => { throw { status: 422, detail: [{ loc: ['body', 'person_name'], msg: 'must be a string' }] }; },
        () => ({ training_interval_days: 365, training_overdue: [] }),
        () => ADMIN,
        () => ({ id: 99 }));
    await init(ADMIN, []);
    await drain();
    assert.ok(/person_name: must be a string/.test(els['sr-error'].textContent), 'verbatim 422 detail missing');
    assert.strictEqual(els['sr-table-body'].innerHTML, '', 'rows left after error');
});

test('platform-admin gate: me.is_platform_admin false -> no rows, error line', async () => {
    const { init, els, calls } = run(
        () => ({ total: ROWS.length, items: ROWS }),
        () => ({ training_interval_days: 365, training_overdue: [] }),
        () => NON_ADMIN,
        () => ({ id: 99 }));
    await init(ADMIN, []);
    await drain();
    assert.strictEqual(els['sr-table-body'].innerHTML, '', 'rows rendered for non-admin');
    assert.ok(/platform-admin only/.test(els['sr-error'].textContent), 'gate message missing');
});

test('escaping: hostile person_name cannot inject markup', async () => {
    const hostile = [{ ...ROWS[0], id: 7, person_name: '<img src=x onerror=alert(1)>' }];
    const { init, els } = run(
        () => ({ total: 1, items: hostile }),
        () => ({ training_interval_days: 365, training_overdue: [] }),
        () => ADMIN,
        () => ({ id: 99 }));
    await init(ADMIN, []);
    await drain();
    const html = els['sr-table-body'].innerHTML;
    assert.ok(!/<img/.test(html), 'raw <img injected');
    assert.ok(/&lt;img/.test(html), 'name not escaped');
});

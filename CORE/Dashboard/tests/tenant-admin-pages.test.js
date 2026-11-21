// Node-runnable check for the tenantDsar / accessReviews helpers added to
// Dashboard/js/api.js (GSEC R-120 + GY.C10).
//
// api.js is an ES module, so this script strips the `export` keywords, runs
// the body in a vm sandbox with a stubbed fetch (captures endpoint +
// options), and asserts each helper issues the exact endpoint, method and
// JSON body the routers/tenant/ endpoints expect — same technique as
// admin-sso-lock.test.js. No network, no DOM beyond the top-level
// `window.APP_CONFIG` read.
//
//   node --test CORE/Dashboard/tests/tenant-admin-pages.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');
const body = src
    .replace(/^export \{ [^\n]*\};\s*$/m, '')
    .replace(/^export /gm, '');
assert.ok(!/^\s*export /m.test(body), 'unexpected ESM export left');

let calls = [];
const sandbox = {
    window: { APP_CONFIG: {} },
    document: {},
    console,
    sessionStorage: { getItem: () => 'fake-token' },
    fetch: (url, options) => {
        calls.push({ url, options });
        return Promise.resolve({
            status: 200,
            ok: true,
            headers: { get: () => 'application/json' },
            json: async () => ({}),
        });
    },
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(body, sandbox, { filename: 'api.js' });

async function run(driver) {
    return vm.runInContext(`(async () => { ${driver} })()`, sandbox);
}

test('tenantDsar.exportBundle hits GET /tenant/dsar/export with optional tenant_id', async () => {
    calls = [];
    await run(`await tenantDsar.exportBundle();`);
    assert.strictEqual(calls.length, 1);
    assert.ok(calls[0].url.endsWith('/tenant/dsar/export'), calls[0].url);
    assert.ok(!calls[0].options || !calls[0].options.method);

    await run(`await tenantDsar.exportBundle(7);`);
    assert.ok(calls[1].url.endsWith('/tenant/dsar/export?tenant_id=7'), calls[1].url);
});

test('tenantDsar.requestErasure POSTs {tenant_id} when given, {} otherwise', async () => {
    calls = [];
    await run(`await tenantDsar.requestErasure();`);
    assert.ok(calls[0].url.endsWith('/tenant/dsar/erasure'));
    assert.strictEqual(calls[0].options.method, 'POST');
    assert.deepStrictEqual(JSON.parse(calls[0].options.body), {});

    await run(`await tenantDsar.requestErasure(9);`);
    assert.deepStrictEqual(JSON.parse(calls[1].options.body), { tenant_id: 9 });
});

test('tenantDsar.confirmErasure POSTs the single-use token', async () => {
    calls = [];
    await run(`await tenantDsar.confirmErasure('req-123', 'tok-abc');`);
    assert.strictEqual(calls.length, 1);
    assert.ok(calls[0].url.endsWith('/tenant/dsar/erasure/req-123/confirm'));
    assert.strictEqual(calls[0].options.method, 'POST');
    assert.deepStrictEqual(JSON.parse(calls[0].options.body), { token: 'tok-abc' });
});

test('accessReviews campaign lifecycle endpoints', async () => {
    calls = [];
    await run(`await accessReviews.listCampaigns();`);
    assert.ok(calls[0].url.endsWith('/tenant/access-reviews/campaigns'));
    await run(`await accessReviews.get(42);`);
    assert.ok(calls[1].url.endsWith('/tenant/access-reviews/campaigns/42'));
    await run(`await accessReviews.sodReport();`);
    assert.ok(calls[2].url.endsWith('/tenant/access-reviews/sod-report'));
});

test('accessReviews.create sends scope and only schedule_due_at when set', async () => {
    calls = [];
    await run(`await accessReviews.create({ title: 'Q4', scope: 'all_members' });`);
    assert.strictEqual(calls[0].options.method, 'POST');
    assert.deepStrictEqual(JSON.parse(calls[0].options.body),
        { title: 'Q4', scope: 'all_members' });

    await run(`await accessReviews.create({ title: 'Q1', scope: 'resource_grants', schedule_due_at: '2027-01-15T09:00:00.000Z' });`);
    assert.deepStrictEqual(JSON.parse(calls[1].options.body), {
        title: 'Q1', scope: 'resource_grants',
        schedule_due_at: '2027-01-15T09:00:00.000Z',
    });
});

test('accessReviews item sign-off: approve optional comment, revoke required comment', async () => {
    calls = [];
    await run(`await accessReviews.approveItem(1, 2);`);
    assert.ok(calls[0].url.endsWith('/tenant/access-reviews/campaigns/1/items/2/approve'));
    assert.deepStrictEqual(JSON.parse(calls[0].options.body), {});

    await run(`await accessReviews.approveItem(1, 2, 'fine');`);
    assert.deepStrictEqual(JSON.parse(calls[1].options.body), { comment: 'fine' });

    await run(`await accessReviews.revokeItem(1, 2, 'no longer needed');`);
    assert.ok(calls[2].url.endsWith('/tenant/access-reviews/campaigns/1/items/2/revoke'));
    assert.deepStrictEqual(JSON.parse(calls[2].options.body), { comment: 'no longer needed' });

    await run(`await accessReviews.close(1);`);
    assert.ok(calls[3].url.endsWith('/tenant/access-reviews/campaigns/1/close'));
    assert.deepStrictEqual(JSON.parse(calls[3].options.body), {});

    await run(`await accessReviews.close(1, 'reviewed');`);
    assert.deepStrictEqual(JSON.parse(calls[4].options.body), { comment: 'reviewed' });
});

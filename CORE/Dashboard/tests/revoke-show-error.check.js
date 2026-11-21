// FIX-REVOKE-SHOW-ERROR (, D72): the Revoke button in the
// resource-permissions panel (CORE/Dashboard/js/resource-permissions.js) fired
// DELETE for every grant id and NEVER looked at the HTTP status
// (`try { await fetch(...) } catch { /* continue; reload reflects the truth */ }`):
// a 403 (Forbidden via can_manage_resource_grant) or 404 gave no message and the
// panel just reloaded with the row still there - "it said I could but did
// nothing" (DIAG-DELL-PERMISSIONS... F-2). This test drives the REAL click
// handler with a mocked fetch and asserts:
//   200 -> panel reloads (the row is gone), no error message;
//   403 -> showError names the API's `detail`, panel NOT reloaded (row left in
//          place), button re-enabled so the user can retry;
//   404 with a non-JSON body -> showError falls back to the status.
// Red on the pre-fix file (no status check at all), green after the fix.
//   node --test CORE/Dashboard/tests/revoke-show-error.check.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'resource-permissions.js'), 'utf8');

// One scenario: DELETEs answer `status` (jsonBody undefined => body is not JSON),
// the panel GET always answers 200 with one user holding two grants.
function runCase(status, jsonBody) {
    const calls = [];
    const errors = [];
    const responder = (url, opts) => {
        const method = (opts && opts.method) || 'GET';
        calls.push(method);
        if (method !== 'DELETE') {
            return Promise.resolve({ ok: true, status: 200, json: () => ([
                { id: 11, user_id: 7, user_name: 'Ada', capability_display: 'view' },
                { id: 12, user_id: 7, user_name: 'Ada', capability_display: 'edit' },
            ]) });
        }
        if (status === 200) return Promise.resolve({ ok: true, status: 200, json: () => ({}) });
        const resp = {
            ok: false,
            status,
            json: jsonBody === undefined ? () => { throw Error('body not json'); } : () => jsonBody,
        };
        return Promise.resolve(resp);
    };

    const btn = { dataset: { permids: '11,12', username: 'Ada' }, disabled: true, textContent: '', handlers: [] };
    btn.addEventListener = (type, fn) => { if (type === 'click') btn.handlers.push(fn); };
    btn.querySelector = () => null;
    btn.querySelectorAll = () => [];
    const listDiv = { innerHTML: '', querySelector: () => null };
    listDiv.querySelectorAll = (sel) => (sel === '.rp-revoke-btn' ? [btn] : []);
    const container = { innerHTML: '', querySelector: (sel) => (sel === '.rp-list' ? listDiv : null) };
    container.querySelectorAll = () => [];

    const sandbox = {
        window: {
            APP_CONFIG: { API_URL: 'http://x' },
            confirm: () => true,
            showError: (msg) => { errors.push(String(msg)); return Promise.resolve(); },
            showInfo: () => {},
        },
        document: { getElementById: (id) => (id === 'panel' ? container : null) },
        sessionStorage: { getItem: () => 'tok' },
        fetch: responder,
    };
    vm.createContext(sandbox);
    vm.runInContext(src.replace(/^export /gm, '') + '\nglobalThis.__load = loadResourcePermissions;', sandbox);
    const load = vm.runInContext('globalThis.__load', sandbox);
    return { btn, calls, errors, load };
}

test('(a) resource-permissions.js parses and exports the loader', () => {
    assert.match(src, /export async function loadResourcePermissions\(/);
    const classic = src.replace(/^export /gm, '');
    assert.doesNotThrow(() => new vm.Script(classic, { filename: 'resource-permissions.js' }));
});

test('(b) 200 DELETEs: panel reloads, no error message', async () => {
    const c = runCase(200, {});
    await c.load('panel', 'secret', 1, 'Top Secret');
    assert.strictEqual(c.btn.handlers.length, 1);
    await c.btn.handlers[0]();
    // initial GET + reload GET, two DELETEs
    assert.strictEqual(c.calls.filter((m) => m === 'GET').length, 2);
    assert.strictEqual(c.calls.filter((m) => m === 'DELETE').length, 2);
    assert.strictEqual(c.errors.length, 0);
});

test('(c) 403 DELETEs: showError names the detail, row left in place, button re-enabled', async () => {
    const c = runCase(403, { detail: "You can't revoke this grant." });
    await c.load('panel', 'secret', 1, 'Top Secret');
    await c.btn.handlers[0]();
    assert.strictEqual(c.errors.length, 1);
    assert.match(c.errors[0], /You can't revoke this grant\./);
    // no reload: the row stays where it is
    assert.strictEqual(c.calls.filter((m) => m === 'GET').length, 1);
    assert.strictEqual(c.btn.disabled, false);
    assert.strictEqual(c.btn.textContent, 'Revoke');
});

test('(d) 404 DELETEs with a non-JSON body: showError names the status', async () => {
    const c = runCase(404);
    await c.load('panel', 'secret', 1, 'Top Secret');
    await c.btn.handlers[0]();
    assert.strictEqual(c.errors.length, 1);
    assert.match(c.errors[0], /404/);
    assert.strictEqual(c.calls.filter((m) => m === 'GET').length, 1);
});

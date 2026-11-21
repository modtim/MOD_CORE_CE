// Node-runnable check for the FX.152 p4 Dashboard API-keys Rotate button +
// Expires column + Stale badge in Dashboard/js/settings.js (paired with the
// POST /api-keys/{id}/rotate endpoint in API/routers/api_keys.py).
//
// settings.js is an ES module, so this script extracts the contiguous API-keys
// block (from the "API Keys Functions" banner down to the GE.AIC.5 GPU note)
// and runs it in a vm sandbox with stubbed showConfirm / showInfo / showError
// / formatDate / apiKeys.rotate / document, then asserts:
//   (a) golden path: confirm -> rotate called, new key pushed into the
//       create-flow reveal display, grace toast, list reloaded, button re-enabled;
//   (b) cancel path: declining the confirm calls nothing;
//   (c) error path: the failure message is surfaced in the error toast,
//       button re-enabled, no reveal;
//   (d) in-flight guard: the button is disabled while the request runs;
//   (e) render: every row gets a Rotate button, Expires shows "Never" when
//       expires_at is null, the Stale badge only when the API marks the key.
//
//   node --test CORE/Dashboard/tests/api-keys-rotate.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');
const start = src.indexOf('// API Keys Functions');
const end = src.indexOf('// GE.AIC.5 (2026-09-29)');
assert.ok(start >= 0 && end > start, 'could not locate the API-keys block');
const block = src.slice(start, end);

// same escape table as Dashboard/js/settings.js:12 (_skEsc)
const skEsc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function run(rotate, confirm) {
    const calls = { rotate: [], info: [], error: [], get: [] };
    const elements = {
        'api-keys-tbody': { innerHTML: '' },
        'new-api-key-display': { classList: { add: () => {}, remove: () => {} } },
        'new-api-key-value': { textContent: '' },
    };
    const button = {
        disabled: false,
        textContent: 'Rotate',
        classList: { add: () => {}, remove: () => {} },
        getAttribute: () => '42',
    };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        document: {
            getElementById: (id) => elements[id] || null,
            querySelectorAll: () => [],
        },
        showConfirm: () => Promise.resolve(confirm),
        showInfo: (msg, title) => { calls.info.push(msg); },
        showError: (msg, title) => { calls.error.push(msg); },
        formatDate: (d) => String(d),
        apiKeys: {
            get: () => { calls.get.push(true); return Promise.resolve([]); },
            rotate: (id) => {
                calls.rotate.push(id);
                return rotate(id);
            },
        },
    };
    const handler = vm.runInNewContext(
        `"use strict";\n${block}\nhandleRotateApiKey;`, sandbox,
        { filename: 'api-keys-block' });
    return { handler, calls, button, elements };
}

test('golden path: confirm rotates, reveals new key, toasts grace, reloads', async () => {
    const { handler, calls, button, elements } = run(
        () => Promise.resolve({
            id: 77,
            access_key: 'sk_mod_live_newsecret',
            old_key_id: 42,
            grace_until: '2026-10-07T12:00:00Z',
        }), true);
    await handler({ target: button });
    assert.strictEqual(calls.rotate.length, 1, 'rotate not called');
    assert.strictEqual(calls.rotate[0], '42');
    assert.strictEqual(elements['new-api-key-value'].textContent,
        'sk_mod_live_newsecret', 'new key not revealed');
    assert.strictEqual(calls.info.length, 1, 'no grace toast');
    assert.ok(/2026-10-07T12:00:00Z/.test(calls.info[0]), 'grace_until not surfaced');
    assert.strictEqual(calls.get.length, 1, 'list not reloaded');
    assert.strictEqual(button.disabled, false, 'button left disabled');
});

test('cancel path: declining the confirm calls nothing', async () => {
    const { handler, calls, button, elements } = run(() => ({}), false);
    await handler({ target: button });
    assert.strictEqual(calls.rotate.length, 0, 'rotate called after cancel');
    assert.strictEqual(calls.info.length, 0);
    assert.strictEqual(calls.error.length, 0);
    assert.strictEqual(elements['new-api-key-value'].textContent, '', 'reveal touched');
    assert.strictEqual(button.disabled, false);
});

test('error path: failure message surfaced, no reveal, button re-armed', async () => {
    const { handler, calls, button, elements } = run(
        () => Promise.reject(new Error('API key is disabled; rotate only live keys.')), true);
    await handler({ target: button });
    assert.strictEqual(calls.rotate.length, 1);
    assert.strictEqual(calls.error.length, 1, 'no error toast');
    assert.ok(/rotate only live keys/.test(calls.error[0]), 'detail not surfaced');
    assert.strictEqual(calls.info.length, 0);
    assert.strictEqual(elements['new-api-key-value'].textContent, '', 'reveal touched');
    assert.strictEqual(button.disabled, false, 'button left disabled after failure');
});

test('in-flight guard: button disabled during the round-trip', async () => {
    const { handler, calls, button } = run(() => new Promise(() => {}), true);
    const pending = handler({ target: button });
    await new Promise((drain) => setTimeout(drain, 0)); // drain all microtasks
    assert.strictEqual(button.disabled, true, 'not disabled while in flight');
    assert.strictEqual(calls.rotate.length, 1);
    void pending; // never resolves: the guard is the point of this test
});

test('render: Rotate button per row, Expires "Never", Stale badge only when flagged', async () => {
    const renderStart = block.indexOf('function renderApiKeys');
    // openCreateApiKeyModal is `async function` now: cut before the modifier too
    const renderEnd = block.search(/(async\s+)?function openCreateApiKeyModal/);
    assert.ok(renderStart >= 0 && renderEnd > renderStart, 'could not locate renderApiKeys');
    const renderBlock = block.slice(renderStart, renderEnd);
    const elements = { 'api-keys-tbody': { innerHTML: '' } };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        document: {
            getElementById: (id) => elements[id] || null,
            querySelectorAll: () => [],
        },
        formatDate: (d) => String(d),
        loadResourcePermissions: () => {},
        _skEsc: skEsc,
    };
    const render = vm.runInNewContext(
        `"use strict";\n${renderBlock}\nrenderApiKeys;`, sandbox,
        { filename: 'render-api-keys' });
    render([
        { id: 1, key_prefix: 'sk_a', description: 'live', created_at: 'c', last_used_at: 'u', expires_at: '2026-12-01T00:00:00Z', stale: true },
        { id: 2, key_prefix: 'sk_b', description: null, created_at: 'c', last_used_at: null, expires_at: null, stale: false },
    ]);
    const html = elements['api-keys-tbody'].innerHTML;
    assert.ok(/rotate-api-key-btn/.test(html), 'no Rotate button');
    assert.ok((html.match(/rotate-api-key-btn/g) || []).length === 2, 'Rotate button not per-row');
    assert.ok(/2026-12-01T00:00:00Z/.test(html), 'expires_at not rendered');
    assert.ok(/>Never</.test(html), 'null expires_at must show Never');
    assert.ok((html.match(/>Stale</g) || []).length === 1, 'Stale badge must appear only for stale keys');
});

test('escape: description/prefix are HTML-escaped, no raw markup in tbody', async () => {
    const renderStart = block.indexOf('function renderApiKeys');
    const renderEnd = block.search(/(async\s+)?function openCreateApiKeyModal/);
    assert.ok(renderStart >= 0 && renderEnd > renderStart, 'could not locate renderApiKeys');
    const renderBlock = block.slice(renderStart, renderEnd);
    const elements = { 'api-keys-tbody': { innerHTML: '' } };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        document: {
            getElementById: (id) => elements[id] || null,
            querySelectorAll: () => [],
        },
        formatDate: (d) => String(d),
        loadResourcePermissions: () => {},
        _skEsc: skEsc,
    };
    const render = vm.runInNewContext(
        `"use strict";\n${renderBlock}\nrenderApiKeys;`, sandbox,
        { filename: 'render-api-keys-escape' });
    render([
        { id: 3, key_prefix: 'ab"c', description: '<img src=x onerror=alert(1)>',
          created_at: 'c', last_used_at: null, expires_at: null, stale: false },
        { id: 4, key_prefix: 'sk_plain', description: null, created_at: 'c',
          last_used_at: null, expires_at: null, stale: false },
    ]);
    const html = elements['api-keys-tbody'].innerHTML;
    assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'),
        'description not escaped');
    assert.ok(!html.includes('<img src=x'), 'raw markup reached innerHTML');
    assert.ok(html.includes('data-key-name="ab&quot;c...'), 'prefix attribute not escaped');
    assert.ok(html.includes('>No description<'), 'null description lost its placeholder');
});

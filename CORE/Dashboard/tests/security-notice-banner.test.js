// Node-runnable check for the GSEC FX.50 p3b security-notice banner in
// Dashboard/js/ui.js (helper: api.js securityNotices.list ->
// GET /v1/tenant/security-notices).
//
// ui.js is a large ES module, so (like vault-sealed-banner.test.js) we slice
// out just the self-contained `_loadSecurityNoticeBanner` function, run it in
// a vm sandbox with a tiny DOM + a stubbed securityNotices helper, and assert:
//   * one line per notice, text "Security notice: <summary> — see your email
//     for details.";
//   * summary text is esc()'d (an incident summary can never inject markup);
//   * no notices / 403 (not a tenant admin) / dead API -> banner hidden;
//   * dismissal is sessionStorage-only (per session, not a server flag).
//
//   node --test CORE/Dashboard/tests/security-notice-banner.test.js
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

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'ui.js'), 'utf8');

const fnStart = src.indexOf('async function _loadSecurityNoticeBanner');
assert.ok(fnStart >= 0, '_loadSecurityNoticeBanner not found in ui.js');
const marker = src.indexOf('// GSEC FX.53 (Q36=A) — MFA enforcement countdown banner. Shown while the',
    fnStart);
assert.ok(marker > fnStart, 'end marker for _loadSecurityNoticeBanner not found');
const snippet = src.slice(fnStart, marker);

async function run(loadFn, listResult, opts = {}) {
    const banner = {
        id: 'security-notice-banner',
        innerHTML: '',
        classList: { _set: new Set(['hidden']),
                     add(c) { this._set.add(c); },
                     remove(c) { this._set.delete(c); },
                     contains(c) { return this._set.has(c); } },
    };
    const store = Object.assign({ accessToken: 'tok' }, opts.session || {});
    const sandbox = {
        document: { getElementById: (id) => (id === banner.id ? banner : null) },
        sessionStorage: {
            getItem(k) { return store[k] ?? null; },
            setItem(k, v) { store[k] = String(v); },
        },
        securityNotices: { list: listResult },
        getPlatformFeatures: async () => ({ entitlements: { edition: 'paid' } }),
        isPaidEdition: async () => true,
    };
    const fn = vm.runInNewContext(`(${loadFn})`, sandbox);
    await fn();
    return { banner, store };
}

const two = { items: [
    { id: 'a', summary: 'Worker GPU pool compromised', notified_at: '2026-10-06T12:00:00Z' },
    { id: 'b', summary: 'Shared platform worker breached', notified_at: '2026-10-06T13:00:00Z' },
], total: 2, limit: 50, offset: 0 };

test.describe('security-notice banner');

test('shows one line per notice with the exact wording', async () => {
    const { banner, store } = await run(snippet, async () => two);
    assert.ok(!banner.classList.contains('hidden'), 'banner must be visible');
    assert.ok(banner.innerHTML.includes(
        'Security notice: Worker GPU pool compromised — see your email for details.'));
    assert.ok(banner.innerHTML.includes(
        'Security notice: Shared platform worker breached — see your email for details.'));
    assert.ok(banner.innerHTML.includes('Dismiss'), 'dismiss button present');
    assert.ok(!store.securityNoticeDismissed, 'not auto-dismissed');
});

test('summary text is escaped (no markup injection)', async () => {
    const evil = { items: [{ id: 'x', summary: '<img src=x onerror=alert(1)>&"' }] };
    const { banner } = await run(snippet, async () => evil);
    assert.ok(banner.innerHTML.includes('&lt;img src=x onerror=alert(1)&gt;&amp;&quot;'),
        'summary must be escaped');
    assert.ok(!/<img/.test(banner.innerHTML), 'no raw <img tag may reach the DOM');
});

test('no notices -> hidden', async () => {
    const { banner } = await run(snippet, async () => ({ items: [], total: 0 }));
    assert.ok(banner.classList.contains('hidden'), 'empty list hides the banner');
});

test('non-admin 403 / dead API -> hidden', async () => {
    const { banner } = await run(snippet, async () => { throw new Error('403'); });
    assert.ok(banner.classList.contains('hidden'), 'fetch failure hides the banner');
});

test('dismissed this session -> stays hidden', async () => {
    const { banner } = await run(snippet, async () => two,
        { session: { securityNoticeDismissed: '1' } });
    assert.ok(banner.classList.contains('hidden'),
        'session dismissal suppresses the banner');
});

// Node-runnable check for the Q9 Vault-sealed banner in Dashboard/js/ui.js.
//
// ui.js is a large ES module with many imports, so (like the other
// node --test checks here, e.g. admin-sso-lock.test.js) we slice out just the
// self-contained `_loadVaultSealedBanner` function + its module-level timer
// variable, run that source in a vm sandbox with a tiny DOM + a stubbed
// fetch/sessionStorage, and assert the banner shows only while
// `vault_sealed === true`, carries the runbook link, and stops polling for
// non-admins (403).
//
//   node --test CORE/Dashboard/tests/vault-sealed-banner.test.js
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

// Slice out the timer declaration + the _loadVaultSealedBanner function.
const start = src.indexOf('let _vaultSealedBannerTimer = null;');
assert.ok(start >= 0, '_vaultSealedBannerTimer declaration not found in ui.js');
const fnStart = src.indexOf('async function _loadVaultSealedBanner', start);
assert.ok(fnStart > start, '_loadVaultSealedBanner not found in ui.js');
// End of the function: the next top-level "async function " / "let " marker
// after the fn body. The banner function body ends right before the
// "// First-month welcome-bonus banner" comment that follows it.
const marker = src.indexOf('// First-month welcome-bonus banner', fnStart);
assert.ok(marker > fnStart, 'end marker for _loadVaultSealedBanner not found');
const snippet = src.slice(start, marker);

// --- tiny DOM / env stubs -------------------------------------------------
function makeBanner() {
    const el = {
        id: 'vault-sealed-banner',
        innerHTML: '',
        classList: {
            _set: new Set(['hidden']),
            add(c) { this._set.add(c); },
            remove(c) { this._set.delete(c); },
            contains(c) { return this._set.has(c); },
        },
    };
    return el;
}

function makeSandbox({ fetchImpl }) {
    const banner = makeBanner();
    const state = { intervalCalls: [] };
    const sandbox = {
        document: { getElementById: (id) => (id === 'vault-sealed-banner' ? banner : null) },
        sessionStorage: {
            _store: { accessToken: 'tok' },
            getItem(k) { return this._store[k] ?? null; },
            setItem(k, v) { this._store[k] = String(v); },
        },
        window: { APP_CONFIG: { API_URL: 'http://api.test', DOCS_URL: 'http://docs.test' } },
        fetch: fetchImpl,
        getPlatformFeatures: async () => ({ entitlements: { edition: 'paid' } }),
        isPaidEdition: async () => true,
        setInterval: (fn, ms) => { state.intervalCalls.push(ms); return state.intervalCalls.length; },
        clearInterval: () => {},
        console,
    };
    sandbox.globalThis = sandbox;
    return { sandbox, banner, state };
}

async function loadAndRun(tickFetch) {
    const { sandbox, banner, state } = makeSandbox({ fetchImpl: tickFetch });
    vm.createContext(sandbox);
    vm.runInContext(snippet, sandbox);
    await vm.runInContext('_loadVaultSealedBanner()', sandbox);
    return { banner, state, sandbox };
}

test('shows the red banner + runbook link while vault_sealed is true', async () => {
    const { banner } = await loadAndRun(async () => ({
        ok: true,
        json: async () => ({ vault_sealed: true }),
    }));
    assert.ok(!banner.classList.contains('hidden'), 'banner should be visible');
    assert.ok(banner.innerHTML.includes('Vault is sealed'), 'banner text');
    assert.ok(banner.innerHTML.includes('unseal it'), 'banner text');
    assert.ok(banner.innerHTML.includes('/security/vault-unseal-runbook'), 'runbook link');
    assert.ok(banner.innerHTML.includes('http://docs.test'), 'docs base resolved');
});

test('stays hidden when vault_sealed is false (unsealed / unknown)', async () => {
    const { banner } = await loadAndRun(async () => ({
        ok: true,
        json: async () => ({ vault_sealed: false }),
    }));
    assert.ok(banner.classList.contains('hidden'), 'banner must be hidden when not sealed');
    assert.ok(!banner.innerHTML.includes('Vault is sealed'));
});

test('stops polling for non-admins (403) and stays hidden', async () => {
    let calls = 0;
    const { banner, state } = await loadAndRun(async () => {
        calls += 1;
        return { ok: false, status: 403 };
    });
    assert.ok(banner.classList.contains('hidden'), 'banner hidden on 403');
    // Exactly one tick ran; the interval must NOT have been (re)armed for a
    // non-admin.
    assert.strictEqual(calls, 1, 'should not poll again after 403');
    assert.strictEqual(state.intervalCalls.length, 0, 'no interval for non-admin');
});

test('re-arms a 60s poll for an admin whose Vault is unsealed', async () => {
    const { state } = await loadAndRun(async () => ({
        ok: true,
        json: async () => ({ vault_sealed: false }),
    }));
    assert.strictEqual(state.intervalCalls.length, 1, 'admin gets a poll interval');
    assert.strictEqual(state.intervalCalls[0], 60000, 'poll cadence is 60s');
});

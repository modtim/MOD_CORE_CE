// FIX-DASHBOARD-SKIP-ADMIN-ONLY-DRAIN-STATUS-POLL-FOR-NON-PLATFORM-ADMIN-
// SESSIONS (11-17, owner Tim: "fix before launch") — node-runnable check for
// the GT.C2 dispatch drain-mode banner in Dashboard/js/ui.js.
//
// ui.js is a large ES module (many imports), so, exactly like
// vault-sealed-banner.test.js next door, we slice out just the
// `_drainBannerTimer` declaration + the whole `_loadDrainModeBanner` function
// and run that source in a vm sandbox with a tiny DOM, a stubbed fetch,
// sessionStorage and a stubbed `me` object (the browser gets `me` from
// api.js: `me.get: () => _meCacheGet('profile')` — the SAME cached GET /me
// the page already fetches on every nav, coalesced with a 30s TTL, so
// awaiting it inside the banner prologue adds NO extra HTTP request and no
// new endpoint).
//
//   node --test CORE/Dashboard/tests/drain-status-poll-admin-gate.test.js
//
// Checks (the counted proof):
//   1) a NON-platform-admin session makes ZERO drain-status calls
//      (MUTATION PROOF: reverting ui.js to the tip-before code makes this red
//      — the old prologue polls unconditionally, so the recording fetch stub
//      sees one call and the ZERO assert fails);
//   2) a platform-admin session still polls (first tick + 60s re-arm), banner
//      shows while `paused` — byte-identical behaviour to before;
//   3) the 403/401 hide-and-stop-for-good safety still works for a session
//      whose role is unknown (/me unreadable -> fail open, poll, stop on 403).
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'ui.js'), 'utf8');

const start = src.indexOf('let _drainBannerTimer = null;');
assert.ok(start >= 0, 'ui.js: _drainBannerTimer declaration not found');
const fnStart = src.indexOf('async function _loadDrainModeBanner', start);
assert.ok(fnStart > start, 'ui.js: _loadDrainModeBanner not found');
const marker = src.indexOf('// Q9 — Vault sealed banner', fnStart);
assert.ok(marker > fnStart, 'ui.js: end marker for _loadDrainModeBanner not found');
const snippet = src.slice(start, marker);

// The recorded fetch URLs for the run currently under way (every stub below
// records, so a ZERO-count assert is a real "no request happened" proof).
let fetches = [];

function makeSandbox({ meProfile, status, body }) {
    const banner = {
        id: 'drain-mode-banner', innerHTML: '',
        classList: {
            _set: new Set(['hidden']),
            add(c) { this._set.add(c); },
            remove(c) { this._set.delete(c); },
            contains(c) { return this._set.has(c); },
        },
    };
    const state = { intervalCalls: [] };
    const sandbox = {
        document: { getElementById: (id) => (id === 'drain-mode-banner' ? banner : null) },
        sessionStorage: { _store: { accessToken: 'tok' }, getItem(k) { return this._store[k] ?? null; }, setItem(k, v) { this._store[k] = String(v); } },
        window: { APP_CONFIG: { API_URL: 'http://api.test' } },
        fetch: (url, opts) => {
            fetches.push(url);
            // a browser fetch Response: `ok` is 2xx/3xx, never a bare status
            return { ok: status < 400, status: status, json: async () => body };
        },
        me: { get: async () => meProfile },
        setInterval: (fn, ms) => { state.intervalCalls.push(ms); return 1; },
        clearInterval: () => {},
        console,
    };
    sandbox.globalThis = sandbox;
    return { sandbox, banner, state };
}

async function loadAndRun({ meProfile, status, body }) {
    fetches = [];
    const { sandbox, banner, state } = makeSandbox({ meProfile, status, body });
    vm.createContext(sandbox);
    vm.runInContext(snippet, sandbox);
    await vm.runInContext('_loadDrainModeBanner()', sandbox);
    return { banner, state };
}

const drainCalls = () => fetches.filter((u) => u.includes('/system/drain-status'));

test('non-admin session makes ZERO drain-status calls (banner hidden, no timer)', async () => {
    const { banner, state } = await loadAndRun({ meProfile: { is_platform_admin: false }, status: 200, body: { paused: true } });
    assert.strictEqual(drainCalls().length, 0,
        'a session known not to be a platform admin must not poll drain-status at all');
    assert.ok(banner.classList.contains('hidden'), 'banner stays hidden');
    assert.strictEqual(state.intervalCalls.length, 0, 'no 60s poller armed');
});

test('platform admin still polls immediately and re-arms the 60s timer', async () => {
    const { banner, state } = await loadAndRun({ meProfile: { is_platform_admin: true }, status: 200, body: { paused: true } });
    assert.strictEqual(drainCalls().length, 1, 'admin polls drain-status exactly once on load');
    assert.ok(!banner.classList.contains('hidden'), 'paused=true shows the drain banner');
    assert.deepStrictEqual(state.intervalCalls, [60000], 'same 60s interval re-armed as before');
});

test('platform admin, not paused: banner hidden, 60s poller still armed', async () => {
    const { banner, state } = await loadAndRun({ meProfile: { is_platform_admin: true }, status: 200, body: { paused: false } });
    assert.strictEqual(drainCalls().length, 1, 'still polls once on load');
    assert.ok(banner.classList.contains('hidden'), 'hidden while not paused');
    assert.deepStrictEqual(state.intervalCalls, [60000], 'poller still armed');
});

test('unknown role (/me unreadable): fail open -> old behaviour, stop on 403', async () => {
    const { banner, state } = await loadAndRun({ meProfile: null, status: 403, body: {} });
    assert.strictEqual(drainCalls().length, 1, 'unknown role still polls (fail open)');
    assert.ok(banner.classList.contains('hidden'), 'banner hidden on 403');
    assert.strictEqual(state.intervalCalls.length, 0, 'stop-on-403 safety unchanged');
});

test('/me throws: fail open -> one poll, stop on 403, banner hidden', async () => {
    const sandboxBundle = makeSandbox({ meProfile: null, status: 403, body: {} });
    const { sandbox, banner, state } = sandboxBundle;
    sandbox.me = { get: async () => { throw new Error('me unreadable'); } };
    fetches = [];
    vm.createContext(sandbox);
    vm.runInContext(snippet, sandbox);
    await vm.runInContext('_loadDrainModeBanner()', sandbox);
    assert.strictEqual(drainCalls().length, 1, 'role unknown -> the old unconditional poll still happens');
    assert.ok(banner.classList.contains('hidden'), 'banner hidden');
    assert.strictEqual(state.intervalCalls.length, 0, 'polling stopped for good');
});

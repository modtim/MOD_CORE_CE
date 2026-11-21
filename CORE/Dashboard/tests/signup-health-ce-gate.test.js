// node:test sandbox test — Signup Health CE-gate (isPaidEdition blocks the panel)
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

// ---------------------------------------------------------------------------
// Helpers — makeEl with style.display, drain, drainAll
// ---------------------------------------------------------------------------

function makeEl(id) {
    const el = {
        id, value: '', disabled: false, className: '',
        textContent: '', innerHTML: '', _hidden: false,
        dataset: {}, listeners: {},
        style: { display: '' },
    };
    el.classList = {
        add: (c) => { if (c === 'hidden') el._hidden = true; },
        remove: (c) => { if (c === 'hidden') el._hidden = false; },
        toggle: () => {},
    };
    el.querySelectorAll = () => [];
    el.addEventListener = (type, fn) => { el.listeners[type] = fn; };
    el.textContent = el._text = '';
    return el;
}

async function drain(n = 30) {
    for (let i = 0; i < n; i++) await Promise.resolve();
}

// ---------------------------------------------------------------------------
// Read source and extract the two function blocks
// ---------------------------------------------------------------------------

const src = fs.readFileSync(
    path.join(__dirname, '..', 'js', 'users.js'),
    'utf8',
);

// Extract from `async function maybeLoadSignupHealth` through end of
// `loadSignupOrphansPanel` (the caller of the API).
// Strip the `isPaidEdition` import line so the sandbox stub takes effect.
// Start at _shEsc (the helper loadSignupOrphansPanel needs in its error path) so
// the sandbox defines it; otherwise the HEAD "Couldn't load" path throws
// ReferenceError and case (e) would pass for the wrong reason.
const mStart = src.indexOf('function _shEsc(s)');
const loadStart = src.indexOf('async function loadSignupOrphansPanel()');
// Slice from maybeLoadSignupHealth THROUGH the end of loadSignupOrphansPanel
// (loadSignupOrphansPanel is the last function in users.js), so the sandbox
// defines BOTH functions and the paid path can actually reach the API call.
const mBlock = src.slice(mStart).replace(/export /g, '');
const mBlockNoImport = mBlock
    .split('\n')
    .filter((line) => {
        // Remove only the import declaration that brings in isPaidEdition, not the guard usage
        return !(line.trim().startsWith('import {') && line.includes('isPaidEdition'));
    })
    .join('\n');

// ---------------------------------------------------------------------------
// Case (a): CE edition → section hidden, ZERO API calls
// ---------------------------------------------------------------------------

test('signup-health: CE edition hides panel — 0 apiCall, display=none', async () => {
    const els = {};
    els['signup-health-section'] = makeEl('signup-health-section');
    els['signup-orphans-tbody'] = makeEl('signup-orphans-tbody');
    els['signup-orphans-count'] = makeEl('signup-orphans-count');
    els['signup-orphans-refresh'] = makeEl('signup-orphans-refresh');

    let apiCallCount = 0;
    let meCalls = 0;
    const apiCallCalls = [];
    const sandbox = {
        console: { log: () => {}, warn: () => {} },
        document: { getElementById: (id) => els[id] || null },
        apiCall: (url) => { apiCallCount++; apiCallCalls.push(url); return Promise.resolve({ items: [] }); },
        me: { get: () => { meCalls++; return Promise.resolve({ is_platform_admin: true }); } },
        isPaidEdition: () => Promise.resolve(false),
    };
    const init = vm.runInNewContext(`"use strict";\n${mBlockNoImport}\nmaybeLoadSignupHealth;`, sandbox);
    await init();
    await drain(50);

    assert.strictEqual(els['signup-health-section'].style.display, 'none',
        'CE: signup-health-section display must be none');
    assert.strictEqual(apiCallCount, 0,
        'CE: apiCall must NOT be called (0 calls)');
    assert.strictEqual(meCalls, 0,
        'CE: me.get must NOT be called (the isPaidEdition guard must precede the /me fetch)');
});

// ---------------------------------------------------------------------------
// Case (b): paid + is_platform_admin true → panel shown, ONE apiCall
// ---------------------------------------------------------------------------

test('signup-health: paid + platform admin shows panel — 1 apiCall', async () => {
    const els = {};
    els['signup-health-section'] = makeEl('signup-health-section');
    els['signup-orphans-tbody'] = makeEl('signup-orphans-tbody');
    els['signup-orphans-count'] = makeEl('signup-orphans-count');
    els['signup-orphans-refresh'] = makeEl('signup-orphans-refresh');

    let apiCallCount = 0;
    const apiCallCalls = [];
    const sandbox = {
        console: { log: () => {}, warn: () => {} },
        document: { getElementById: (id) => els[id] || null },
        apiCall: (url) => { apiCallCount++; apiCallCalls.push(url); return Promise.resolve({ items: [] }); },
        me: { get: () => Promise.resolve({ is_platform_admin: true }) },
        isPaidEdition: () => Promise.resolve(true),
    };
    const init = vm.runInNewContext(`"use strict";\n${mBlockNoImport}\nmaybeLoadSignupHealth;`, sandbox);
    await init();
    await drain(50);

    assert.strictEqual(els['signup-health-section'].style.display, '',
        'paid+admin: display must be empty string');
    assert.strictEqual(apiCallCount, 1,
        'paid+admin: apiCall called exactly once');
    assert.strictEqual(apiCallCalls[0], '/platform/signup-orphans',
        'paid+admin: apiCall URL must be /platform/signup-orphans');
});

// ---------------------------------------------------------------------------
// Case (c): paid + is_platform_admin false → hidden, ZERO calls
// ---------------------------------------------------------------------------

test('signup-health: paid but not admin hides panel — 0 apiCall', async () => {
    const els = {};
    els['signup-health-section'] = makeEl('signup-health-section');
    els['signup-orphans-tbody'] = makeEl('signup-orphans-tbody');
    els['signup-orphans-count'] = makeEl('signup-orphans-count');
    els['signup-orphans-refresh'] = makeEl('signup-orphans-refresh');

    let apiCallCount = 0;
    const sandbox = {
        console: { log: () => {}, warn: () => {} },
        document: { getElementById: (id) => els[id] || null },
        apiCall: (url) => { apiCallCount++; return Promise.resolve({ items: [] }); },
        me: { get: () => Promise.resolve({ is_platform_admin: false }) },
        isPaidEdition: () => Promise.resolve(true),
    };
    const init = vm.runInNewContext(`"use strict";\n${mBlockNoImport}\nmaybeLoadSignupHealth;`, sandbox);
    await init();
    await drain(50);

    assert.strictEqual(els['signup-health-section'].style.display, 'none',
        'paid+not-admin: display must be none');
    assert.strictEqual(apiCallCount, 0,
        'paid+not-admin: apiCall must NOT be called');
});

// ---------------------------------------------------------------------------
// Case (d): isPaidEdition THROWS → fail-closed: hidden, 0 calls
// ---------------------------------------------------------------------------

test('signup-health: isPaidEdition throws — fail closed, 0 apiCall', async () => {
    const els = {};
    els['signup-health-section'] = makeEl('signup-health-section');
    els['signup-orphans-tbody'] = makeEl('signup-orphans-tbody');
    els['signup-orphans-count'] = makeEl('signup-orphans-count');
    els['signup-orphans-refresh'] = makeEl('signup-orphans-refresh');

    let apiCallCount = 0;
    let meCalls = 0;
    const sandbox = {
        console: { log: () => {}, warn: () => {} },
        document: { getElementById: (id) => els[id] || null },
        apiCall: (url) => { apiCallCount++; return Promise.resolve({ items: [] }); },
        me: { get: () => { meCalls++; return ({ is_platform_admin: true }); } },
        isPaidEdition: () => { throw new Error('features endpoint unavailable'); },
    };
    const init = vm.runInNewContext(`"use strict";\n${mBlockNoImport}\nmaybeLoadSignupHealth;`, sandbox);
    await init();
    await drain(50);

    assert.strictEqual(els['signup-health-section'].style.display, 'none',
        'throws: display must be none (fail closed)');
    assert.strictEqual(apiCallCount, 0,
        'throws: apiCall must NOT be called');
    assert.strictEqual(meCalls, 0,
        'throws: me.get must NOT be called before the edition check');
});

// ---------------------------------------------------------------------------
// Case (e): CE + apiCall would reject with "Not Found" — no "Couldn't load"
// This confirms the fix eliminates the Tim-visible "Couldn't load - Not Found" text.
// ---------------------------------------------------------------------------

test('signup-health: CE rejects with Not Found — no "Couldn\'t load" in tbody', async () => {
    const els = {};
    els['signup-health-section'] = makeEl('signup-health-section');
    els['signup-orphans-tbody'] = makeEl('signup-orphans-tbody');
    els['signup-orphans-count'] = makeEl('signup-orphans-count');
    els['signup-orphans-refresh'] = makeEl('signup-orphans-refresh');

    const sandbox = {
        console: { log: () => {}, warn: () => {} },
        document: { getElementById: (id) => els[id] || null },
        apiCall: () => Promise.reject(new Error('Not Found')),
        // admin=true so the HEAD (ungated) code actually reaches the fetch and
        // writes "Couldn't load — Not Found" into the tbody; the new CE guard
        // must stop it before any call.
        me: { get: () => Promise.resolve({ is_platform_admin: true }) },
        isPaidEdition: () => false,
    };
    const init = vm.runInNewContext(`"use strict";\n${mBlockNoImport}\nmaybeLoadSignupHealth;`, sandbox);
    await init();
    await drain(50);

    assert.ok(!els['signup-orphans-tbody'].innerHTML.includes("Couldn't load"),
        'CE reject: tbody must NOT contain "Couldn\'t load"');
});

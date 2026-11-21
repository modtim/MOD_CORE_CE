// Node-runnable check for Dashboard/js/entitlements.js (GT.D11a).
//
// The Dashboard has no JS unit-test runner and entitlements.js is an ES
// module (imports ./api.js), so this script strips the import/export lines,
// runs the body in a vm sandbox with a stubbed getPlatformFeatures and a
// tiny DOM, and asserts has()/lockElement() behavior — same spirit as
// burst-accounts.check.js / ps-sidecar-filter.check.js.
//
//   node CORE/Dashboard/tests/entitlements.test.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(
    path.join(__dirname, '..', 'js', 'entitlements.js'), 'utf8');

// Strip ESM-only lines; the rest is plain script that binds onto `window`.
const body = src
    .replace(/^import .* from '\.\/api\.js';\s*$/m, '')
    .replace(/^export default .*$/m, '');
assert.ok(!/^\s*(import|export) /m.test(body), 'unexpected ESM syntax left');

// --- tiny DOM stub ------------------------------------------------------
function makeEl(id) {
    const classes = new Set();
    const children = [];
    const el = {
        id: id || '',
        disabled: false,
        title: '',
        dataset: {},
        children,
        classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
            toggle: (c, force) => {
                const on = force === undefined ? !classes.has(c) : !!force;
                if (on) classes.add(c); else classes.delete(c);
            },
        },
        appendChild: (c) => { children.push(c); return c; },
        insertAdjacentElement: (how, node) => {
            assert.strictEqual(how, 'afterend');
            children.push(node);
            return node;
        },
        addEventListener: () => {},
        textContent: '',
    };
    // Test-only accessor so assertions can read the live class set.
    el._classes = classes;
    return el;
}

const document = { createElement: (tag) => makeEl(tag) };

function runSandbox(featuresPayload, featuresThrow) {
    const sandbox = {
        window: {},
        document,
        Date,
        getPlatformFeatures: () => {
            if (featuresThrow) return Promise.reject(new Error('offline'));
            return Promise.resolve(featuresPayload);
        },
        console,
    };
    sandbox.window.window = sandbox.window;
    vm.createContext(sandbox);
    vm.runInContext(body, sandbox, { filename: 'entitlements.js' });
    return sandbox.window.MODEntitlements;
}

const CE = {
    edition: 'ce',
    tier_name: 'Core',
    source: 'baseline',
    capabilities: ['custom_nodes', 'show_timing', 'sso', 'stack_edit'],
    limits: { max_tenants: 1, max_manager_nodes: 'unlimited' },
};
const PRO = { ...CE, edition: 'pro', capabilities: [...CE.capabilities, 'marketplace_publish', 'predictive'] };

(async () => {
    // 1. has(): present vs absent, plus fail-open when the block is missing.
    let m = runSandbox({ entitlements: CE });
    assert.strictEqual(await m.has('marketplace_publish'), false, 'CE lacks marketplace_publish');
    assert.strictEqual(await m.has('stack_edit'), true, 'CE has stack_edit');
    assert.strictEqual(await m.edition(), 'ce');
    assert.strictEqual(await m.tier(), 'Core');
    assert.strictEqual(await m.limit('max_tenants'), 1);
    assert.strictEqual(await m.limit('max_manager_nodes'), 'unlimited');
    assert.strictEqual(await m.limit('nope'), null, 'unknown limit is null');

    m = runSandbox({ entitlements: PRO });
    assert.strictEqual(await m.has('marketplace_publish'), true, 'pro grants marketplace_publish');
    assert.strictEqual(await m.edition(), 'pro');

    m = runSandbox({}, false); // no entitlements block at all
    assert.strictEqual(await m.has('marketplace_publish'), false, 'missing block fails CLOSED');

    m = runSandbox(undefined, true); // fetch rejected
    assert.strictEqual(await m.has('marketplace_publish'), false, 'fetch failure fails CLOSED');

    // 2. lockElement(): absent cap → locked + badge; present cap → untouched.
    m = runSandbox({ entitlements: CE });
    const locked = makeEl('btn');
    await m.lockElement(locked, 'marketplace_publish', 'Publish to the Marketplace');
    assert.ok(locked._classes.has('mod-locked'), 'locked element gets .mod-locked');
    assert.strictEqual(locked.disabled, true, 'locked element is disabled');
    assert.ok(/not part of this edition/i.test(locked.title), 'title tooltip set');
    assert.strictEqual(locked.dataset.modLocked, '1');
    assert.strictEqual(locked.children.length, 1, 'one badge appended');
    const badge = locked.children[0];
    assert.strictEqual(badge.className, 'mod-paid-badge');
    assert.strictEqual(badge.children[0].textContent, 'Not part of this edition');
    assert.strictEqual(badge.children[0].href, '#billing', 'badge links to upgrade page');

    // Idempotent: a second call must not stack a second badge.
    await m.lockElement(locked, 'marketplace_publish');
    assert.strictEqual(locked.children.length, 1, 'lockElement is idempotent');

    // Granted cap → element untouched.
    const free = makeEl('btn2');
    await m.lockElement(free, 'stack_edit');
    assert.ok(!free._classes.has('mod-locked'), 'granted cap leaves element alone');
    assert.strictEqual(free.disabled, false);
    assert.strictEqual(free.children.length, 0);

    // null element is a no-op.
    await m.lockElement(null, 'marketplace_publish');

    console.log('entitlements.test.js: all assertions passed');
})().catch((e) => {
    console.error('FAIL:', e.message);
    process.exit(1);
});

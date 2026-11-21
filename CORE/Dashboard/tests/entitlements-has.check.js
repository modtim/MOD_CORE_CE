// rc20-ent-has — behavioral check for has() fail-closed fix.
//
// Same spirit as failopen-gates.check.js: the real entitlements.js is loaded
// into a fresh node:vm sandbox with stubbed getPlatformFeatures and a tiny DOM,
// and EXECUTED so the has() return value is decided by running it.
//
//   node CORE/Dashboard/tests/entitlements-has.check.js
//
// Pinned contract (entitlements.js:45-48):
//   async has(cap) {
//       const b = (await _entitlements()).entitlements;
//       if (!b || !Array.isArray(b.capabilities)) return false; // fail closed
//       return b.capabilities.includes(cap);
//   }
//
//   1) missing block → has() returns false (was true = FAIL-OPEN bug).
//   2) error block (fetch rejected) → has() returns false (was true = FAIL-OPEN).
//   3) a positive feature in the block → has() returns true.
//   4) an absent feature (not in capabilities) → has() returns false.
//
// RED on the HEAD (old): scenarios 1 and 2 returned true → 2 FAIL lines, rc=1.

'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
function read(rel) { return fs.readFileSync(path.join(ROOT, rel), 'utf8'); }

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

function parsesModule(rel) {
    try {
        execFileSync(process.execPath, ['--input-type=module', '--check'],
            { input: read(rel), encoding: 'utf8' });
        return true;
    } catch (e) { return false; }
}

// Strip ESM-only lines; the rest is plain script that binds onto `window`.
function stripImports(src) {
    return src
        .replace(/^import .* from '\.\/api\.js';\s*$/m, '')
        .replace(/^export default .*$/m, '');
}

// --- tiny DOM stub (lockElement needs it) --------------------------------
function makeEl(id) {
    const classes = new Set();
    const children = [];
    const el = {
        id, disabled: false, title: '', dataset: {}, children,
        classList: {
            add(c) { classes.add(c); },
            remove(c) { classes.delete(c); },
            contains(c) { return classes.has(c); },
            toggle(c, on) { if (on) classes.add(c); else classes.delete(c); },
        },
        appendChild(c) { children.push(c); return c; },
        insertAdjacentElement(how, node) { children.push(node); return node; },
        addEventListener() {},
        textContent: '',
        _classes: classes,
    };
    return el;
}

// One fresh sandbox per scenario: stub getPlatformFeatures and run.
function runSandbox(featuresPayload, featuresThrow) {
    const sandbox = {
        window: {},
        document: { createElement: (tag) => makeEl(tag) },
        Date,
        getPlatformFeatures: () => {
            if (featuresThrow) return Promise.reject(new Error('offline'));
            return Promise.resolve(featuresPayload);
        },
        console,
    };
    sandbox.window.window = sandbox.window;
    vm.createContext(sandbox);
    vm.runInContext(stripImports(read('js/entitlements.js')), sandbox,
        { filename: 'entitlements.js' });
    return sandbox.window.MODEntitlements;
}

(async () => {
    check('entitlements.js parses as a valid ES module', parsesModule('js/entitlements.js'));

    // 1) missing block → has() MUST be false (fail closed).
    let m = runSandbox({}, false);
    check('missing block → has("marketplace_publish") is false',
        await m.has('marketplace_publish') === false);

    // 2) error block (fetch rejected) → has() MUST be false.
    m = runSandbox(undefined, true);
    check('fetch failure → has("marketplace_publish") is false',
        await m.has('marketplace_publish') === false);

    // 3) a positive feature → has() returns true.
    const CE = {
        edition: 'ce', tier_name: 'Core', source: 'baseline',
        capabilities: ['custom_nodes', 'sso', 'stack_edit'],
        limits: { max_tenants: 1 },
    };
    m = runSandbox({ entitlements: CE }, false);
    check('positive feature "sso" → has("sso") is true',
        await m.has('sso') === true);

    // 4) absent feature → has() returns false.
    check('absent feature "marketplace_publish" → has() is false',
        await m.has('marketplace_publish') === false);

    // 5) lockElement respects fail-closed: missing block → element gets locked.
    m = runSandbox({}, false);
    const locked = makeEl('btn');
    await m.lockElement(locked, 'marketplace_publish', 'Publish');
    check('missing block lockElement → .mod-locked class present',
        locked._classes.has('mod-locked'));
    check('missing block lockElement → element disabled', locked.disabled);

    console.log(`\nentitlements-has.check.js: ${failures ? 'FAILED (' + failures + ')' : 'all asserts passed'}`);
    process.exit(failures ? 1 : 0);
})().catch((err) => { console.error('FAIL ' + String(err && err.message)); process.exit(1); });

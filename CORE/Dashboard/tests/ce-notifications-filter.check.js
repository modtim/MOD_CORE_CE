// FIX-CE-NOTIFICATIONS-FILTER-GUARD — notifications filter guard check.
// Static + vm: extracts currentFilter() from js/notifications.js and runs it
// against a stub localStorage and a stub chip set (no browser, no DOM lib).
//
//   node CORE/Dashboard/tests/ce-notifications-filter.check.js
//
// Checks:
//   0) js/notifications.js parses (node --check on the module source).
//   1) CE: stored 'billing' with no billing chip -> 'all', and the stale value is cleared.
//   2) stored 'unread' / 'workflows' with the chip rendered -> unchanged, not cleared.
//   3) stored 'all' -> 'all'.
//   4) paid: stored 'billing' with the billing chip rendered -> 'billing' unchanged.
//   5) no chips rendered at all -> stored value kept (cannot judge, do not clear).
//   6) localStorage unavailable -> 'all'.
// MUTATION proof: with the guard removed (MCW_OVERRIDE), check 1 must fail.

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const FILE = path.join(ROOT, 'js', 'notifications.js');
const src = fs.readFileSync(FILE, 'utf8');

// 0) parses as an ES module
let syntaxErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'], { input: src, encoding: 'utf8' });
} catch (e) { syntaxErr = String(e && (e.stderr || e.message)); }
assert.ok(!syntaxErr, `notifications.js not valid ES module — ${(syntaxErr || '').split('\n')[0] || 'unknown'}`);

// Extract the storage key const and the currentFilter() function text.
const keyMatch = src.match(/const FILTER_STORAGE_KEY = '[^']+';/);
assert.ok(keyMatch, 'FILTER_STORAGE_KEY const must exist');
const fnStart = src.indexOf('function currentFilter() {');
assert.ok(fnStart >= 0, 'function currentFilter() must exist');
const fnEnd = src.indexOf('\n}\n', fnStart);
assert.ok(fnEnd > fnStart, 'currentFilter() must end with a column-0 closing brace');
const fnSrc = src.slice(fnStart, fnEnd + 3);

// Stubs: a Map-backed localStorage and a chip list shaped like the DOM nodes.
function makeEnv({ stored, chips, storageThrows = false }) {
    const store = new Map();
    if (stored !== undefined) store.set('mod.dashNotifFilter.v1', stored);
    const localStorage = {
        getItem: (k) => { if (storageThrows) throw new Error('denied'); return store.has(k) ? store.get(k) : null; },
        setItem: (k, v) => { store.set(k, String(v)); },
        removeItem: (k) => { store.delete(k); },
    };
    const nodes = (chips || []).map((f) => ({ dataset: { filter: f } }));
    const document = { querySelectorAll: () => nodes };
    const ctx = vm.createContext({ localStorage, document, console });
    vm.runInContext(`${keyMatch[0]}\n${fnSrc}\nthis.__currentFilter = currentFilter;`, ctx);
    return { run: () => ctx.__currentFilter(), store };
}

const CE_CHIPS = ['all', 'unread', 'workflows', 'nodes', 'archived']; // billing chip scrubbed on CE
const PAID_CHIPS = ['all', 'unread', 'workflows', 'nodes', 'billing', 'archived'];

// 1) CE stale billing value falls back to 'all' and is cleared
{
    const env = makeEnv({ stored: 'billing', chips: CE_CHIPS });
    assert.strictEqual(env.run(), 'all', "CE: stored 'billing' with no billing chip must return 'all'");
    assert.ok(!env.store.has('mod.dashNotifFilter.v1'), 'CE: stale stored filter must be cleared');
}
// 2) chip present -> unchanged, not cleared
for (const v of ['unread', 'workflows', 'nodes', 'archived']) {
    const env = makeEnv({ stored: v, chips: CE_CHIPS });
    assert.strictEqual(env.run(), v, `CE: stored '${v}' with its chip must be unchanged`);
    assert.strictEqual(env.store.get('mod.dashNotifFilter.v1'), v, `CE: stored '${v}' must not be cleared`);
}
// 3) stored 'all' -> 'all'
{
    const env = makeEnv({ stored: 'all', chips: CE_CHIPS });
    assert.strictEqual(env.run(), 'all', "stored 'all' must return 'all'");
}
// 4) paid: billing chip rendered -> 'billing' unchanged (byte-identical behaviour)
{
    const env = makeEnv({ stored: 'billing', chips: PAID_CHIPS });
    assert.strictEqual(env.run(), 'billing', "paid: stored 'billing' with billing chip must return 'billing'");
    assert.strictEqual(env.store.get('mod.dashNotifFilter.v1'), 'billing', 'paid: stored billing must not be cleared');
}
// 5) no chips rendered -> keep stored value
{
    const env = makeEnv({ stored: 'billing', chips: [] });
    assert.strictEqual(env.run(), 'billing', 'no chips rendered: stored value must be kept');
    assert.strictEqual(env.store.get('mod.dashNotifFilter.v1'), 'billing', 'no chips rendered: value must not be cleared');
}
// 6) storage unavailable -> 'all'
{
    const env = makeEnv({ stored: 'billing', chips: PAID_CHIPS, storageThrows: true });
    assert.strictEqual(env.run(), 'all', 'storage throws: must return all');
}
// nothing stored -> 'all'
{
    const env = makeEnv({ stored: undefined, chips: CE_CHIPS });
    assert.strictEqual(env.run(), 'all', 'nothing stored: must return all');
}

console.log('ce-notifications-filter.check.js: all checks passed');
